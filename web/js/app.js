// web/js/app.js

// ---- CONFIG ----

// Where to load per-component status files from.
const WORDPRESS_STATUS_BASE = "/sugar_house_monitor/data";
const LOCAL_STATUS_BASE = "/data";
const STATUS_BASE_URL =
  window.STATUS_URL_OVERRIDE ||
  (window.location.hostname.includes("mattsmaplesyrup.com") ||
  window.location.pathname.includes("/sugar_house_monitor")
    ? WORDPRESS_STATUS_BASE
    : LOCAL_STATUS_BASE);

const TANK_STATUS_FILES = {
  brookside: "status_brookside.json",
  roadside: "status_roadside.json"
};
const PUMP_STATUS_FILE = "status_pump.json";
const VACUUM_STATUS_FILE = "status_vacuum.json";
const MONITOR_STATUS_FILE = "status_monitor.json";
const FLOW_HISTORY_ENDPOINT =
  window.location.hostname.includes("mattsmaplesyrup.com") ||
  window.location.pathname.includes("/sugar_house_monitor")
    ? "/sugar_house_monitor/api/flow_history.php"
    : "/api/flow_history.php";
const FLOW_WINDOWS = {
  "10800": "3h",
  "21600": "6h",
  "43200": "12h",
  "86400": "24h",
  "259200": "3d",
  "604800": "7d",
  "1209600": "14d",
};

// Flow thresholds (gph) and reserve volume (gal)
const TANKS_FILLING_THRESHOLD = Number(window.TANKS_FILLING_THRESHOLD ?? 5);
const TANKS_EMPTYING_THRESHOLD = Number(window.TANKS_EMPTYING_THRESHOLD ?? -10);
const RESERVE_GALLONS = Number(window.RESERVE_GALLONS ?? 150);

// Staleness thresholds in seconds (server-time-based, but we approximate with browser time).
// You can tune these as needed.
const STALE_THRESHOLDS = {
  tank_brookside: 120,  // 2 minutes
  tank_roadside:  120,  // 2 minutes
  pump:           7200  // 2 hours
};

const MONITOR_STALE_SECONDS = 150; // 2.5 minutes

// How often to refetch status files (in ms)
const FETCH_INTERVAL_MS = 1_000; // 15s
const FLOW_HISTORY_DEFAULT_SEC = 6 * 60 * 60; // 6h
let flowHistoryWindowSec = FLOW_HISTORY_DEFAULT_SEC;

// How often to recompute "seconds since last" and update the UI (in ms)
const STALENESS_UPDATE_MS = 5_000; // 5s

// ---- STATE ----

let latestTanks = { brookside: null, roadside: null };
let latestPump = null;
let latestVacuum = null;
let latestMonitor = null;
let lastGeneratedAt = null;
let lastFetchError = false;
let lastPumpFlow = null;
const pumpHistory = [];
const netFlowHistory = [];
const HISTORY_WINDOW_MS = 6 * 60 * 60 * 1000; // 6 hours
const HISTORY_MIN_SPACING_MS = 30 * 1000; // throttle points every 30s unless value changes

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
  return `${Math.round(num)} gal`;
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

function formatDurationHhMm(minutes) {
  if (minutes == null || !isFinite(minutes) || minutes < 0) return "---";
  const total = Math.max(0, minutes);
  const hrs = Math.floor(total / 60);
  const mins = Math.floor(total % 60);
  return `${String(hrs).padStart(2, "0")}:${String(mins).padStart(2, "0")}`;
}

function formatProjectedTime(minutes, referenceIso) {
  if (minutes == null || !isFinite(minutes) || minutes < 0) return "---";
  const ref = parseIso(referenceIso) || new Date();
  const ts = new Date(ref.getTime() + minutes * 60 * 1000);
  const mm = String(ts.getMonth() + 1).padStart(2, "0");
  const dd = String(ts.getDate()).padStart(2, "0");
  const hh = String(ts.getHours()).padStart(2, "0");
  const mi = String(ts.getMinutes()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${mi}`;
}

function formatHoursAgo(seconds) {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return null;
  return (seconds / 3600).toFixed(1);
}

function msFromIso(ts) {
  const d = parseIso(ts);
  return d ? d.getTime() : null;
}

function averageMs(a, b) {
  if (a != null && b != null) return (a + b) / 2;
  return a != null ? a : b;
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

function derivePumpStatus(evtType) {
  if (!evtType) return "Unknown";
  if (typeof evtType === "string" && evtType.toLowerCase() === "pump stop") {
    return "Not pumping";
  }
  return "Pumping";
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
  // Gracefully handle empty or malformed JSON by returning null.
  const text = await res.text();
  if (!text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch (err) {
    console.warn(`Failed to parse ${file}:`, err);
    return null;
  }
}

async function fetchHistory() {
  const url = `${FLOW_HISTORY_ENDPOINT}?window_sec=${flowHistoryWindowSec}`;
  const res = await fetch(url, { cache: "no-store" });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`HTTP ${res.status} for flow_history`);
  const text = await res.text();
  if (!text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch (err) {
    console.warn("Failed to parse flow_history:", err);
    return null;
  }
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
  const fillElem = document.getElementById(`${prefix}-fill`);

  if (!tankData) {
    if (volElem) volElem.textContent = "–";
    if (capElem) capElem.textContent = "Capacity: –";
    if (flowElem) flowElem.textContent = "–";
    if (etaElem) etaElem.textContent = "ETA full/empty: –";
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

  if (volElem) volElem.textContent = formatVolumeGal(vol);
  if (capElem) capElem.textContent = cap != null ? `Capacity: ${formatVolumeGal(cap)}` : "Capacity: –";
  if (flowElem) flowElem.textContent = formatFlowGph(flow);
  if (etaElem) etaElem.textContent = formatEta(tankData.eta_full, tankData.eta_empty);

  if (fillElem) {
    const h = pct != null ? Math.max(0, Math.min(100, pct)) : 0;
    fillElem.style.height = `${h}%`;
  }

  // Update card-level warning for a single tank? We instead handle combined info in header.
}

function updatePumpCard(pumpData, staleSec, thresholdSec) {
  const typeElem = document.getElementById("pump-event-type");
  const timeElem = document.getElementById("pump-last-time");
  const flowElem = document.getElementById("pump-flow");
  const runElem  = document.getElementById("pump-run-summary");

  if (!pumpData) {
    if (typeElem) typeElem.textContent = "–";
    if (timeElem) timeElem.textContent = "Time: –";
    if (flowElem) flowElem.textContent = "–";
    if (runElem) runElem.textContent = "Run time / interval: –";
    return;
  }

  const evtType = pumpData.event_type || "–";
  const evtTime = pumpData.last_event_timestamp;
  const runTime = toNumber(pumpData.pump_run_time_s);
  const interval = toNumber(pumpData.pump_interval_s);
  const gph = toNumber(pumpData.gallons_per_hour);

  const statusText = pumpData.pump_status || derivePumpStatus(evtType);

  if (typeElem) typeElem.textContent = statusText;
  if (timeElem) timeElem.textContent = `Time: ${formatDateTime(evtTime)}`;
  if (flowElem) flowElem.textContent = formatFlowGph(gph);

  if (runElem) {
    if (runTime != null && interval != null) {
      runElem.textContent = `Run: ${runTime.toFixed(0)} s / Interval: ${interval.toFixed(0)} s`;
    } else {
      runElem.textContent = "Run --- / Interval ---";
    }
  }
}

function computeOverviewSummary() {
  const brookside = latestTanks.brookside;
  const roadside = latestTanks.roadside;
  if (!brookside && !roadside) return null;

  const bVol = toNumber(brookside?.volume_gal);
  const rVol = toNumber(roadside?.volume_gal);
  const bCap = toNumber(brookside?.max_volume_gal ?? brookside?.capacity_gal);
  const rCap = toNumber(roadside?.max_volume_gal ?? roadside?.capacity_gal);
  const bFlow = toNumber(brookside?.flow_gph);
  const rFlow = toNumber(roadside?.flow_gph);

  const totalGallons = (bVol ?? 0) + (rVol ?? 0);
  const hasFlow = bFlow != null || rFlow != null;
  const netFlow = hasFlow ? (bFlow || 0) + (rFlow || 0) : null;

  const roadRemaining = rCap != null && rVol != null ? Math.max(rCap - rVol, 0) : null;
  const brookRemaining = bCap != null && bVol != null ? Math.max(bCap - bVol, 0) : null;

  let overflowMinutes = null;
  if (rFlow != null && rFlow >= TANKS_FILLING_THRESHOLD && roadRemaining != null && rFlow > 0) {
    overflowMinutes = (roadRemaining / rFlow) * 60;
  } else if (bFlow != null && bFlow >= TANKS_FILLING_THRESHOLD) {
    const combinedRemaining = (brookRemaining ?? 0) + (roadRemaining ?? 0);
    if (netFlow != null && netFlow > 0 && combinedRemaining > 0) {
      overflowMinutes = (combinedRemaining / netFlow) * 60;
    }
  }

  let lastFireMinutes = null;
  if (netFlow != null && netFlow <= TANKS_EMPTYING_THRESHOLD) {
    const available = Math.max(totalGallons - RESERVE_GALLONS, 0);
    if (Math.abs(netFlow) > 0) {
      lastFireMinutes = (available / Math.abs(netFlow)) * 60;
    }
  }

  return {
    totalGallons,
    netFlow,
    overflowMinutes,
    lastFireMinutes,
  };
}

function updateOverviewCard(summary, vacuumData) {
  const totalElem = document.getElementById("overview-total-gallons");
  const netFlowElem = document.getElementById("overview-net-flow");
  const overflowTimeElem = document.getElementById("overview-overflow-time");
  const overflowEtaElem = document.getElementById("overview-overflow-eta");
  const lastFireTimeElem = document.getElementById("overview-last-fire-time");
  const lastFireEtaElem = document.getElementById("overview-last-fire-eta");
  const reserveElem = document.getElementById("overview-reserve");
  const vacReadingElem = document.getElementById("vacuum-reading");
  if (reserveElem) reserveElem.textContent = `${RESERVE_GALLONS} gal`;

  if (!summary) {
    if (totalElem) totalElem.textContent = "–";
    if (netFlowElem) netFlowElem.textContent = "–";
    if (overflowTimeElem) overflowTimeElem.textContent = "---";
    if (overflowEtaElem) overflowEtaElem.textContent = "---";
    if (lastFireTimeElem) lastFireTimeElem.textContent = "---";
    if (lastFireEtaElem) lastFireEtaElem.textContent = `--- (${RESERVE_GALLONS} gal reserve)`;
  } else {
    if (totalElem) totalElem.textContent = formatVolumeGal(summary.totalGallons);
    if (netFlowElem) netFlowElem.textContent = formatFlowGph(summary.netFlow);
    if (overflowTimeElem) overflowTimeElem.textContent = formatDurationHhMm(summary.overflowMinutes);
    if (overflowEtaElem) overflowEtaElem.textContent = formatProjectedTime(summary.overflowMinutes, lastGeneratedAt);
    if (lastFireTimeElem) lastFireTimeElem.textContent = formatDurationHhMm(summary.lastFireMinutes);
    if (lastFireEtaElem) {
      const eta = formatProjectedTime(summary.lastFireMinutes, lastGeneratedAt);
      lastFireEtaElem.textContent = `${eta} (${RESERVE_GALLONS} gal reserve)`;
    }
  }

  const vacVal = toNumber(vacuumData?.reading_inhg);
  if (vacReadingElem) {
    vacReadingElem.textContent = vacVal != null ? `${vacVal.toFixed(1)} inHg` : "–";
  }
}

function updateMonitorCard(data) {
  const tankStatusElem = document.getElementById("monitor-tank-status");
  const tankNoteElem = document.getElementById("monitor-tank-note");
  const pumpStatusElem = document.getElementById("monitor-pump-status");
  const pumpNoteElem = document.getElementById("monitor-pump-note");

  function apply(elem, noteElem, seconds) {
    if (!elem || !noteElem) return;
    elem.classList.remove("status-good", "status-bad");
    if (seconds == null) {
      elem.textContent = "Unknown";
      noteElem.textContent = "Awaiting heartbeat";
      return;
    }
    const hours = formatHoursAgo(seconds);
    if (seconds <= MONITOR_STALE_SECONDS) {
      elem.textContent = "Online";
      elem.classList.add("status-good");
      noteElem.textContent = "Heartbeat received";
    } else {
      elem.textContent = "Offline";
      elem.classList.add("status-bad");
      noteElem.textContent = hours ? `Last update: ${hours} h ago` : "No recent heartbeat";
    }
  }

  apply(tankStatusElem, tankNoteElem, data?.tankSec);
  apply(pumpStatusElem, pumpNoteElem, data?.pumpSec);
}

function addHistoryPoint(arr, value, tsMs) {
  if (value == null || !isFinite(value)) return;
  if (tsMs == null || !isFinite(tsMs)) return;
  const last = arr[arr.length - 1];
  if (last && tsMs - last.t < HISTORY_MIN_SPACING_MS && last.v === value) {
    return;
  }
  if (last && tsMs < last.t) {
    // Keep history monotonic; drop out-of-order samples.
    return;
  }
  arr.push({ t: tsMs, v: value });
}

function pruneHistory(arr, windowEndMs) {
  const cutoff = windowEndMs - HISTORY_WINDOW_MS;
  while (arr.length && arr[0].t < cutoff) {
    arr.shift();
  }
}

function drawLine(ctx, points, color, x0, x1, yMin, yMax, dims) {
  if (!points.length) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((pt, idx) => {
    const xFrac = (pt.t - x0) / (x1 - x0 || 1);
    const yFrac = (pt.v - yMin) / (yMax - yMin || 1);
    const x = dims.padLeft + xFrac * dims.plotW;
    const y = dims.padTop + (1 - yFrac) * dims.plotH;
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function updatePumpHistoryChart(pumpPoint, netPoint) {
  if (pumpPoint) addHistoryPoint(pumpHistory, pumpPoint.v, pumpPoint.t);
  if (netPoint) addHistoryPoint(netFlowHistory, netPoint.v, netPoint.t);

  const latestTs = Math.max(
    pumpHistory.length ? pumpHistory[pumpHistory.length - 1].t : 0,
    netFlowHistory.length ? netFlowHistory[netFlowHistory.length - 1].t : 0
  );

  if (!latestTs) {
    const canvas = document.getElementById("pump-history-canvas");
    const note = document.getElementById("pump-history-note");
    if (canvas) {
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#1a1f28";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#888";
      ctx.font = "12px sans-serif";
      ctx.fillText("No flow data yet", 10, 20);
    }
    if (note) note.textContent = "Showing last 6 hours";
    return;
  }

  pruneHistory(pumpHistory, latestTs);
  pruneHistory(netFlowHistory, latestTs);

  const canvas = document.getElementById("pump-history-canvas");
  const note = document.getElementById("pump-history-note");
  if (!canvas) return;
  // Fit to container width on each draw for responsiveness.
  const desiredWidth = canvas.clientWidth || canvas.width || 600;
  if (canvas.width !== desiredWidth) {
    canvas.width = desiredWidth;
  }
  const ctx = canvas.getContext("2d");
  const now = latestTs;
  const windowMs = flowHistoryWindowSec * 1000;
  const start = now - windowMs;

  // Layout padding for axes/labels
  const padLeft = 52;
  const padRight = 10;
  const padTop = 10;
  const padBottom = 30;
  const plotW = canvas.width - padLeft - padRight;
  const plotH = canvas.height - padTop - padBottom;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#1a1f28";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // Grid + labels
  const yMin = 0;
  const yMax = 200;
  ctx.strokeStyle = "rgba(255,255,255,0.12)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  const yTicks = [0, 50, 100, 150, 200];
  yTicks.forEach((val) => {
    const frac = (val - yMin) / (yMax - yMin || 1);
    const y = padTop + plotH - frac * plotH;
    ctx.moveTo(padLeft, y);
    ctx.lineTo(canvas.width - padRight, y);
  });
  for (let i = 0; i <= 6; i++) {
    const x = padLeft + (i / 6) * plotW;
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, padTop + plotH);
  }
  ctx.stroke();

  // Axis labels
  ctx.fillStyle = "#a7afbf";
  ctx.font = "11px system-ui";
  ctx.textBaseline = "middle";
  ctx.textAlign = "right";
  yTicks.forEach((val) => {
    const frac = (val - yMin) / (yMax - yMin || 1);
    const y = padTop + plotH - frac * plotH;
    ctx.fillText(val.toString(), padLeft - 6, y);
  });
  ctx.save();
  ctx.translate(12, padTop + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillText("gph (0–200)", 0, 0);
  ctx.restore();

  ctx.textBaseline = "top";
  ctx.textAlign = "center";
  const endLabel = new Date(now);
  const startLabel = new Date(start);
  const fmt = (d) =>
    `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  ctx.fillText(fmt(startLabel), padLeft + 40, canvas.height - padBottom + 6);
  ctx.fillText(fmt(endLabel), canvas.width - padRight - 40, canvas.height - padBottom + 6);
  ctx.textAlign = "center";
  ctx.fillText(`time (last ${FLOW_WINDOWS[flowHistoryWindowSec.toString()] || "window"})`, padLeft + plotW / 2, canvas.height - padBottom + 6);

  // Lines
  drawLine(ctx, pumpHistory, "#f2a93b", start, now, yMin, yMax, { padLeft, padTop, plotW, plotH });
  drawLine(ctx, netFlowHistory, "#4caf50", start, now, yMin, yMax, { padLeft, padTop, plotW, plotH });

  if ((!pumpHistory.length) && (!netFlowHistory.length)) {
    ctx.fillStyle = "#888";
    ctx.font = "12px sans-serif";
    ctx.fillText("No flow data yet", 10, 20);
    if (note) note.textContent = "Showing last 6 hours";
    return;
  }

  if (note) {
    note.textContent = `Showing last ${FLOW_WINDOWS[flowHistoryWindowSec.toString()] || "window"} (fixed 0–200 gph)`;
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
  const monitor   = latestMonitor;

  const brooksideSec = brookside ? secondsSinceLast(brookside.last_received_at || brookside.last_sample_timestamp) : null;
  const roadsideSec  = roadside  ? secondsSinceLast(roadside.last_received_at  || roadside.last_sample_timestamp)  : null;
  const pumpSec      = pump      ? secondsSinceLast(pump.last_received_at      || pump.last_event_timestamp)      : null;
  const tankMonitorSec = monitor?.tank_monitor_last_received_at ? secondsSinceLast(monitor.tank_monitor_last_received_at) : null;
  const pumpMonitorSec = monitor?.pump_monitor_last_received_at ? secondsSinceLast(monitor.pump_monitor_last_received_at) : null;

  const brooksideThresh = STALE_THRESHOLDS.tank_brookside;
  const roadsideThresh  = STALE_THRESHOLDS.tank_roadside;
  const pumpThresh      = STALE_THRESHOLDS.pump;

  const brooksideStale = brooksideSec != null && brooksideSec > brooksideThresh;
  const roadsideStale  = roadsideSec  != null && roadsideSec  > roadsideThresh;
  const pumpStale      = pumpSec      != null && pumpSec      > pumpThresh;

  const overview = computeOverviewSummary();
  const pumpFlowVal = toNumber(pump?.gallons_per_hour ?? pump?.flow_gph);
  const pumpTs = msFromIso(pump?.last_event_timestamp || pump?.last_received_at);
  const bTs = msFromIso(brookside?.last_sample_timestamp || brookside?.last_received_at);
  const rTs = msFromIso(roadside?.last_sample_timestamp || roadside?.last_received_at);
  const netTs = averageMs(bTs, rTs);

  updateTankCard("brookside", brookside, brooksideSec, brooksideThresh);
  updateTankCard("roadside",  roadside,  roadsideSec,  roadsideThresh);
  updatePumpCard(pump, pumpSec, pumpThresh);
  updateOverviewCard(overview, latestVacuum);
  updateMonitorCard({
    tankSec: tankMonitorSec,
    pumpSec: pumpMonitorSec,
  });
  updatePumpHistoryChart(
    pumpFlowVal != null && pumpTs != null ? { v: pumpFlowVal, t: pumpTs } : null,
    overview?.netFlow != null && netTs != null ? { v: overview.netFlow, t: netTs } : null
  );

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
    const [brookside, roadside, pumpRaw, vacuum, monitor, history] = await Promise.all([
      fetchStatusFile(TANK_STATUS_FILES.brookside),
      fetchStatusFile(TANK_STATUS_FILES.roadside),
      fetchStatusFile(PUMP_STATUS_FILE),
      fetchStatusFile(VACUUM_STATUS_FILE),
      fetchStatusFile(MONITOR_STATUS_FILE),
      fetchHistory(),
    ]);
    let pump = pumpRaw;
    if (pump && pump.gallons_per_hour == null && lastPumpFlow != null) {
      pump = { ...pump, gallons_per_hour: lastPumpFlow };
    }
    if (pump && pump.gallons_per_hour != null) {
      lastPumpFlow = pump.gallons_per_hour;
    }
    latestTanks = { brookside, roadside };
    latestPump = pump;
    latestVacuum = vacuum;
    latestMonitor = monitor;
    pumpHistory.splice(0, pumpHistory.length);
    netFlowHistory.splice(0, netFlowHistory.length);
    if (history && history.pump) {
      history.pump.forEach((p) => {
        const t = msFromIso(p.ts);
        const v = toNumber(p.flow_gph);
        if (t != null && v != null) pumpHistory.push({ t, v });
      });
    }
    if (history && history.net) {
      history.net.forEach((p) => {
        const t = msFromIso(p.ts);
        const v = toNumber(p.flow_gph);
        if (t != null && v != null) netFlowHistory.push({ t, v });
      });
    }
    lastGeneratedAt = computeLatestGenerated([brookside, roadside, pump, vacuum, monitor]);
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

  const windowSelect = document.getElementById("pump-history-window");
  if (windowSelect) {
    windowSelect.addEventListener("change", () => {
      const val = parseInt(windowSelect.value, 10);
      if (Number.isFinite(val)) {
        flowHistoryWindowSec = val;
        fetchStatusOnce();
      }
    });
  }
}

document.addEventListener("DOMContentLoaded", startLoops);
