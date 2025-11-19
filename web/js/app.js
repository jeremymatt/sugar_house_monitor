// web/js/app.js

// ---- CONFIG ----

// Where to load per-component status files from.
const WORDPRESS_STATUS_BASE = "/sugar_house_monitor/data";
const LOCAL_STATUS_BASE = "/data";
const STATUS_BASE_URL =
  window.STATUS_URL_OVERRIDE ||
  (window.location.pathname.startsWith("/sugar_house_monitor")
    ? WORDPRESS_STATUS_BASE
    : LOCAL_STATUS_BASE);

const TANK_STATUS_FILES = {
  brookside: "status_brookside.json",
  roadside: "status_roadside.json"
};
const PUMP_STATUS_FILE = "status_pump.json";

// Staleness thresholds in seconds (server-time-based, but we approximate with browser time).
// You can tune these as needed.
const STALE_THRESHOLDS = {
  tank_brookside: 120,  // 2 minutes
  tank_roadside:  120,  // 2 minutes
  pump:           7200  // 2 hours
};

// How often to refetch status files (in ms)
const FETCH_INTERVAL_MS = 30_000; // 30s

// How often to recompute "seconds since last" and update the UI (in ms)
const STALENESS_UPDATE_MS = 5_000; // 5s

// ---- STATE ----

let latestTanks = { brookside: null, roadside: null };
let latestPump = null;
let lastGeneratedAt = null;
let lastFetchError = false;

// ---- UTILITIES ----

function parseIso(ts) {
  if (!ts) return null;
  const d = new Date(ts);
  return isNaN(d.getTime()) ? null : d;
}

function formatDateTime(ts) {
  const d = parseIso(ts);
  if (!d) return "unknown";
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  });
}

function formatRelativeSeconds(sec) {
  if (sec == null || !isFinite(sec) || sec < 0) return "unknown";
  if (sec < 60) return `${Math.round(sec)} s ago`;
  const mins = sec / 60;
  if (mins < 60) return `${Math.round(mins)} min ago`;
  const hrs = mins / 60;
  if (hrs < 48) return `${hrs.toFixed(1)} h ago`;
  const days = hrs / 24;
  return `${days.toFixed(1)} days ago`;
}

function formatFlowGph(val) {
  const num = toNumber(val);
  if (num == null) return "–";
  return `${num.toFixed(1)} gph`;
}

function formatVolumeGal(val) {
  const num = toNumber(val);
  if (num == null) return "–";
  if (num >= 1000) return `${(num / 1000).toFixed(2)} kgal`;
  return `${num.toFixed(0)} gal`;
}

function formatPercent(val) {
  const num = toNumber(val);
  if (num == null) return "–";
  return `${num.toFixed(0)}%`;
}

function formatEta(fullEta, emptyEta) {
  const full = fullEta ? `Full: ${formatDateTime(fullEta)}` : null;
  const empty = emptyEta ? `Empty: ${formatDateTime(emptyEta)}` : null;
  if (full && empty) return `${full} / ${empty}`;
  if (full) return full;
  if (empty) return empty;
  return "–";
}

// Compute seconds since last_received_at from browser perspective.
// This assumes server and browser clocks are reasonably close.
function secondsSinceLast(receivedAt) {
  const d = parseIso(receivedAt);
  if (!d) return null;
  const now = Date.now();
  return (now - d.getTime()) / 1000;
}

function toNumber(value) {
  if (value == null || value === "") return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function statusUrlFor(file) {
  const base = STATUS_BASE_URL.endsWith("/")
    ? STATUS_BASE_URL.slice(0, -1)
    : STATUS_BASE_URL;
  return `${base}/${file}`;
}

async function fetchStatusFile(file) {
  const url = statusUrlFor(file);
  const res = await fetch(url, { cache: "no-store" });
  if (res.status === 404) {
    return null;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${file}`);
  return res.json();
}

function computeLatestGenerated(entries) {
  const times = entries
    .map((entry) => {
      if (!entry || !entry.generated_at) return null;
      const parsed = parseIso(entry.generated_at);
      return parsed ? parsed.getTime() : null;
    })
    .filter((t) => t != null);
  if (times.length === 0) return null;
  return new Date(Math.max(...times)).toISOString();
}

// ---- RENDERING ----

function updateTankCard(tankKey, tankData, staleSec, thresholdSec) {
  const prefix = tankKey === "brookside" ? "brookside" : "roadside";

  const volElem = document.getElementById(`${prefix}-volume`);
  const capElem = document.getElementById(`${prefix}-capacity`);
  const flowElem = document.getElementById(`${prefix}-flow`);
  const etaElem = document.getElementById(`${prefix}-eta`);
  const lastElem = document.getElementById(`${prefix}-last-updated`);
  const fillElem = document.getElementById(`${prefix}-fill`);

  if (!tankData) {
    if (volElem) volElem.textContent = "–";
    if (capElem) capElem.textContent = "Capacity: –";
    if (flowElem) flowElem.textContent = "–";
    if (etaElem) etaElem.textContent = "ETA full/empty: –";
    if (lastElem) lastElem.textContent = "Last update: no data";
    if (fillElem) fillElem.style.height = "0%";
    return;
  }

  const vol = toNumber(tankData.volume_gal);
  const cap = toNumber(tankData.max_volume_gal ?? tankData.capacity_gal);
  let pct = toNumber(tankData.level_percent);
  if ((pct == null) && vol != null && cap != null) {
    pct = (vol / cap) * 100;
  }
  const flow = toNumber(tankData.flow_gph);
  const lastTs = tankData.last_sample_timestamp;
  const lastRecv = tankData.last_received_at;

  if (volElem) volElem.textContent = formatVolumeGal(vol);
  if (capElem) capElem.textContent = cap != null ? `Capacity: ${formatVolumeGal(cap)}` : "Capacity: –";
  if (flowElem) flowElem.textContent = formatFlowGph(flow);
  if (etaElem) etaElem.textContent = formatEta(tankData.eta_full, tankData.eta_empty);

  if (fillElem) {
    const h = pct != null ? Math.max(0, Math.min(100, pct)) : 0;
    fillElem.style.height = `${h}%`;
  }

  if (lastElem) {
    const sec = staleSec;
    const rel = formatRelativeSeconds(sec);
    lastElem.textContent = `Last update: ${rel} (server receipt: ${formatDateTime(lastRecv || lastTs)})`;
  }

  // Update card-level warning for a single tank? We instead handle combined info in header.
}

function updatePumpCard(pumpData, staleSec, thresholdSec) {
  const typeElem = document.getElementById("pump-event-type");
  const timeElem = document.getElementById("pump-last-time");
  const flowElem = document.getElementById("pump-flow");
  const runElem  = document.getElementById("pump-run-summary");
  const lastElem = document.getElementById("pump-last-updated");

  if (!pumpData) {
    if (typeElem) typeElem.textContent = "–";
    if (timeElem) timeElem.textContent = "Time: –";
    if (flowElem) flowElem.textContent = "–";
    if (runElem) runElem.textContent = "Run time / interval: –";
    if (lastElem) lastElem.textContent = "Last update: no data";
    return;
  }

  const evtType = pumpData.event_type || "–";
  const evtTime = pumpData.last_event_timestamp;
  const runTime = toNumber(pumpData.pump_run_time_s);
  const interval = toNumber(pumpData.pump_interval_s);
  const gph = toNumber(pumpData.gallons_per_hour);
  const recv = pumpData.last_received_at;

  if (typeElem) typeElem.textContent = evtType;
  if (timeElem) timeElem.textContent = `Time: ${formatDateTime(evtTime)}`;
  if (flowElem) flowElem.textContent = formatFlowGph(gph);

  if (runElem) {
    if (runTime != null && interval != null) {
      runElem.textContent = `Run: ${runTime.toFixed(0)} s / Interval: ${interval.toFixed(0)} s`;
    } else {
      runElem.textContent = "Run time / interval: n/a (non-stop event)";
    }
  }

  if (lastElem) {
    const rel = formatRelativeSeconds(staleSec);
    lastElem.textContent = `Last update: ${rel} (server receipt: ${formatDateTime(recv || evtTime)})`;
  }
}

function updateGlobalStatus(staleInfo) {
  const pill = document.getElementById("global-status-pill");
  const dot  = document.getElementById("global-status-dot");
  const text = document.getElementById("global-status-text");
  const gen  = document.getElementById("generated-at");

  const anyError = staleInfo.error;
  const anyStale = staleInfo.tanksStale || staleInfo.pumpStale;

  if (anyError) {
    if (dot) dot.classList.add("stale");
    if (text) text.textContent = "Error loading status files";
    return;
  }

  if (anyStale) {
    if (dot) dot.classList.add("stale");
    if (text) text.textContent = "Data loaded, but one or more streams are stale";
  } else {
    if (dot) dot.classList.remove("stale");
    if (text) text.textContent = "All streams fresh";
  }

  if (gen) {
    gen.textContent = lastGeneratedAt
      ? `Status generated at: ${formatDateTime(lastGeneratedAt)}`
      : "";
  }
}

function recomputeStalenessAndRender() {
  const brookside = latestTanks.brookside;
  const roadside  = latestTanks.roadside;
  const pump      = latestPump;

  const brooksideSec = brookside ? secondsSinceLast(brookside.last_received_at || brookside.last_sample_timestamp) : null;
  const roadsideSec  = roadside  ? secondsSinceLast(roadside.last_received_at  || roadside.last_sample_timestamp)  : null;
  const pumpSec      = pump      ? secondsSinceLast(pump.last_received_at      || pump.last_event_timestamp)      : null;

  const brooksideThresh = STALE_THRESHOLDS.tank_brookside;
  const roadsideThresh  = STALE_THRESHOLDS.tank_roadside;
  const pumpThresh      = STALE_THRESHOLDS.pump;

  const brooksideStale = brooksideSec != null && brooksideSec > brooksideThresh;
  const roadsideStale  = roadsideSec  != null && roadsideSec  > roadsideThresh;
  const pumpStale      = pumpSec      != null && pumpSec      > pumpThresh;

  updateTankCard("brookside", brookside, brooksideSec, brooksideThresh);
  updateTankCard("roadside",  roadside,  roadsideSec,  roadsideThresh);
  updatePumpCard(pump, pumpSec, pumpThresh);

  const tanksWarning = document.getElementById("tanks-warning");
  const pumpWarning  = document.getElementById("pump-warning");
  if (tanksWarning) {
    tanksWarning.style.display = (brooksideStale || roadsideStale) ? "inline-flex" : "none";
  }
  if (pumpWarning) {
    pumpWarning.style.display = pumpStale ? "inline-flex" : "none";
  }

  const tanksError = document.getElementById("tanks-error");
  const pumpError  = document.getElementById("pump-error");
  if (tanksError) {
    const anyTankData = !!(brookside || roadside);
    tanksError.style.display = anyTankData ? "none" : "block";
  }
  if (pumpError) {
    pumpError.style.display = pump ? "none" : "block";
  }

  const errorState = lastFetchError && !brookside && !roadside && !pump;
  updateGlobalStatus({
    error: errorState,
    tanksStale: brooksideStale || roadsideStale,
    pumpStale: pumpStale
  });
}

// ---- FETCH LOOP ----

async function fetchStatusOnce() {
  try {
    lastFetchError = false;
    const [brookside, roadside, pump] = await Promise.all([
      fetchStatusFile(TANK_STATUS_FILES.brookside),
      fetchStatusFile(TANK_STATUS_FILES.roadside),
      fetchStatusFile(PUMP_STATUS_FILE)
    ]);
    latestTanks = { brookside, roadside };
    latestPump = pump;
    lastGeneratedAt = computeLatestGenerated([brookside, roadside, pump]);
    recomputeStalenessAndRender();
  } catch (err) {
    lastFetchError = true;
    console.error("Failed to fetch status files:", err);
    const tanksError = document.getElementById("tanks-error");
    const pumpError  = document.getElementById("pump-error");
    if (tanksError) tanksError.style.display = "block";
    if (pumpError) pumpError.style.display = "block";
    updateGlobalStatus({
      error: true,
      tanksStale: true,
      pumpStale: true
    });
  }
}

function startLoops() {
  // Initial fetch
  fetchStatusOnce();

  // Periodic refetch
  setInterval(fetchStatusOnce, FETCH_INTERVAL_MS);

  // Staleness recompute even if we don't refetch
  setInterval(recomputeStalenessAndRender, STALENESS_UPDATE_MS);
}

document.addEventListener("DOMContentLoaded", startLoops);
