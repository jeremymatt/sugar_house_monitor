// web/js/app.js

// ---- CONFIG ----

// Force apex domain to avoid CORS/redirect issues if user hits www.
(function enforceApexHost() {
  if (typeof window !== "undefined" && window.location.hostname.startsWith("www.")) {
    const newUrl = window.location.href.replace("://www.", "://");
    window.location.replace(newUrl);
  }
})();

// Where to load per-component status files from.
const WORDPRESS_STATUS_BASE = "/sugar_house_monitor/data";
const LOCAL_STATUS_BASE = "/data";
const STATUS_BASE_URL =
  window.STATUS_URL_OVERRIDE ||
  (window.location.pathname.includes("/sugar_house_monitor") ||
    window.location.hostname.includes("mattsmaplesyrup.com")
    ? WORDPRESS_STATUS_BASE
    : LOCAL_STATUS_BASE);

const TANK_STATUS_FILES = {
  brookside: "status_brookside.json",
  roadside: "status_roadside.json"
};
const PUMP_STATUS_FILE = "status_pump.json";
const EVAP_STATUS_FILE = "status_evaporator.json";
const VACUUM_STATUS_FILE = "status_vacuum.json";
const MONITOR_STATUS_FILE = "status_monitor.json";
const FLOW_HISTORY_ENDPOINT =
  window.location.pathname.includes("/sugar_house_monitor") ||
  window.location.hostname.includes("mattsmaplesyrup.com")
    ? "/sugar_house_monitor/api/flow_history.php"
    : "/api/flow_history.php";
const EVAP_HISTORY_ENDPOINT =
  window.location.pathname.includes("/sugar_house_monitor") ||
  window.location.hostname.includes("mattsmaplesyrup.com")
    ? "/sugar_house_monitor/api/evaporator_history.php"
    : "/api/evaporator_history.php";
const FLOW_WINDOWS = {
  "10800": "3h",
  "21600": "6h",
  "43200": "12h",
  "86400": "24h",
  "259200": "3d",
  "604800": "7d",
  "1209600": "14d",
};
const EVAP_WINDOWS = {
  "3600": "1h",
  "7200": "2h",
  "14400": "4h",
  "21600": "6h",
  "28800": "8h",
  "43200": "12h",
};
const EVAP_Y_MIN_OPTIONS = [0, 100, 200, 300, 400, 500];
const EVAP_Y_MAX_OPTIONS = [300, 400, 500, 600, 700, 800];
const PUMP_Y_MIN_OPTIONS = [0, 50, 100, 150, 200, 250];
const PUMP_Y_MAX_OPTIONS = [50, 100, 150, 200, 300, 500];
const DRAW_OFF_COLORS = {
  brookside: "#4caf50",
  roadside: "#f2a93b",
  "---": "#7a7f8a",
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
const EVAP_HISTORY_DEFAULT_SEC = 2 * 60 * 60; // 2h
let evapHistoryWindowSec = EVAP_HISTORY_DEFAULT_SEC;

// How often to recompute "seconds since last" and update the UI (in ms)
const STALENESS_UPDATE_MS = 5_000; // 5s

// ---- STATE ----

let latestTanks = { brookside: null, roadside: null };
let latestPump = null;
let latestEvaporator = null;
let latestVacuum = null;
let latestMonitor = null;
let lastGeneratedAt = null;
let lastFetchError = false;
let lastPumpFlow = null;
const pumpHistory = [];
const netFlowHistory = [];
let evapHistory = [];
const HISTORY_WINDOW_MS = 6 * 60 * 60 * 1000; // 6 hours
const HISTORY_MIN_SPACING_MS = 30 * 1000; // throttle points every 30s unless value changes
let evapPlotSettings = {
  y_axis_min: 200,
  y_axis_max: 600,
  window_sec: EVAP_HISTORY_DEFAULT_SEC,
};
let pumpYAxisMin = PUMP_Y_MIN_OPTIONS[0];
let pumpYAxisMax = PUMP_Y_MAX_OPTIONS[3]; // default 200
let evapSettingsPending = null;

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

function ensureBounds(minVal, maxVal, minOptions, maxOptions, preferAdjustMin = false) {
  let min = minVal;
  let max = maxVal;
  if (preferAdjustMin) {
    if (max <= min) {
      const nextMin = [...minOptions].reverse().find((opt) => opt < max);
      if (nextMin != null) {
        min = nextMin;
      }
    }
    if (min >= max) {
      const nextMax = maxOptions.find((opt) => opt > min);
      if (nextMax != null) {
        max = nextMax;
      }
    }
  } else {
    if (min >= max) {
      const nextMax = maxOptions.find((opt) => opt > min);
      if (nextMax != null) {
        max = nextMax;
      }
    }
    if (max <= min) {
      const nextMin = [...minOptions].reverse().find((opt) => opt < max);
      if (nextMin != null) {
        min = nextMin;
      }
    }
  }
  return { min, max };
}

function buildTicks(min, max, segments = 4) {
  const step = (max - min) / segments;
  const ticks = [];
  for (let i = 0; i <= segments; i += 1) {
    const val = min + step * i;
    ticks.push(Math.round(val));
  }
  return ticks;
}

function formatTankName(name) {
  if (!name || name === "---") return "---";
  return name.charAt(0).toUpperCase() + name.slice(1);
}

function settingsEqual(a, b) {
  if (!a || !b) return false;
  return (
    Number(a.y_axis_min) === Number(b.y_axis_min) &&
    Number(a.y_axis_max) === Number(b.y_axis_max) &&
    Number(a.window_sec) === Number(b.window_sec)
  );
}

function syncEvapControls() {
  const bounds = ensureBounds(
    evapPlotSettings.y_axis_min,
    evapPlotSettings.y_axis_max,
    EVAP_Y_MIN_OPTIONS,
    EVAP_Y_MAX_OPTIONS
  );
  evapPlotSettings = {
    ...evapPlotSettings,
    y_axis_min: bounds.min,
    y_axis_max: bounds.max,
  };
  const minSel = document.getElementById("boiling-y-min");
  const maxSel = document.getElementById("boiling-y-max");
  const windowSel = document.getElementById("boiling-history-window");
  if (minSel) minSel.value = String(evapPlotSettings.y_axis_min);
  if (maxSel) maxSel.value = String(evapPlotSettings.y_axis_max);
  if (windowSel) windowSel.value = String(evapHistoryWindowSec);
}

function syncPumpControls() {
  const bounds = ensureBounds(pumpYAxisMin, pumpYAxisMax, PUMP_Y_MIN_OPTIONS, PUMP_Y_MAX_OPTIONS);
  pumpYAxisMin = bounds.min;
  pumpYAxisMax = bounds.max;
  const minSel = document.getElementById("pump-y-min");
  const maxSel = document.getElementById("pump-y-max");
  if (minSel) minSel.value = String(pumpYAxisMin);
  if (maxSel) maxSel.value = String(pumpYAxisMax);
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

async function fetchEvaporatorHistory(windowOverrideSec) {
  const windowParam = windowOverrideSec ?? evapHistoryWindowSec;
  const url = `${EVAP_HISTORY_ENDPOINT}?window_sec=${windowParam}`;
  const res = await fetch(url, { cache: "no-store" });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`HTTP ${res.status} for evaporator_history`);
  const text = await res.text();
  if (!text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch (err) {
    console.warn("Failed to parse evaporator_history:", err);
    return null;
  }
}

async function persistEvapSettings(settings) {
  try {
    const res = await fetch(EVAP_HISTORY_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        y_axis_min: settings.y_axis_min,
        y_axis_max: settings.y_axis_max,
        window_sec: settings.window_sec,
      }),
    });
    if (!res.ok) {
      console.warn("Failed to persist evaporator settings:", res.status);
      return null;
    }
    const data = await res.json();
    return data;
  } catch (err) {
    console.warn("Persist evaporator settings error:", err);
    return null;
  }
}

function coerceEvapSettings(raw) {
  if (!raw) return { ...evapPlotSettings };
  const yMin = toNumber(raw.y_axis_min);
  const yMax = toNumber(raw.y_axis_max);
  const windowSec = parseInt(raw.window_sec, 10);
  return {
    y_axis_min: yMin ?? evapPlotSettings.y_axis_min,
    y_axis_max: yMax ?? evapPlotSettings.y_axis_max,
    window_sec: Number.isFinite(windowSec) ? windowSec : evapPlotSettings.window_sec,
  };
}

function applyEvapHistoryResponse(resp) {
  if (!resp) {
    return;
  }
  if (resp.settings) {
    const incoming = coerceEvapSettings(resp.settings);
    if (!evapSettingsPending || settingsEqual(incoming, evapSettingsPending)) {
      evapPlotSettings = incoming;
      evapSettingsPending = null;
    }
  }
  if (resp.window_sec_used && Number.isFinite(resp.window_sec_used)) {
    if (!evapSettingsPending || resp.window_sec_used === evapSettingsPending.window_sec) {
      evapHistoryWindowSec = resp.window_sec_used;
    }
  } else if (evapPlotSettings.window_sec && (!evapSettingsPending || evapPlotSettings.window_sec === evapSettingsPending.window_sec)) {
    evapHistoryWindowSec = evapPlotSettings.window_sec;
  }

  if (Array.isArray(resp.history)) {
    evapHistory = resp.history
      .map((p) => {
        const t = msFromIso(p.ts);
        const v = toNumber(p.evaporator_flow_gph);
        if (t == null || v == null) return null;
        return { t, v, drawOff: p.draw_off_tank || "---" };
      })
      .filter((p) => p != null);
  }

  if (resp.latest && !latestEvaporator) {
    latestEvaporator = resp.latest;
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

function updateBoilingCard(evapData, overview) {
  const flowElem = document.getElementById("boiling-evap-flow");
  const tsElem = document.getElementById("boiling-evap-ts");
  const drawOffElem = document.getElementById("boiling-draw-off");
  const drawOffFlowElem = document.getElementById("boiling-draw-off-flow");
  const pumpInElem = document.getElementById("boiling-pump-in");
  const pumpInFlowElem = document.getElementById("boiling-pump-in-flow");
  const lastFireElem = document.getElementById("boiling-last-fire-time");
  const lastFireEtaElem = document.getElementById("boiling-last-fire-eta");

  const evapFlow = toNumber(evapData?.evaporator_flow_gph);
  if (flowElem) flowElem.textContent = formatFlowGph(evapFlow);
  if (tsElem) tsElem.textContent = evapData?.sample_timestamp ? `Time: ${formatDateTime(evapData.sample_timestamp)}` : "Time: –";

  if (drawOffElem) drawOffElem.textContent = formatTankName(evapData?.draw_off_tank);
  if (drawOffFlowElem) drawOffFlowElem.textContent = `Flow: ${formatFlowGph(toNumber(evapData?.draw_off_flow_gph))}`;

  if (pumpInElem) pumpInElem.textContent = formatTankName(evapData?.pump_in_tank);
  if (pumpInFlowElem) pumpInFlowElem.textContent = `Flow: ${formatFlowGph(toNumber(evapData?.pump_in_flow_gph))}`;

  if (overview) {
    if (lastFireElem) lastFireElem.textContent = formatDurationHhMm(overview.lastFireMinutes);
    if (lastFireEtaElem) {
      const eta = formatProjectedTime(overview.lastFireMinutes, lastGeneratedAt);
      lastFireEtaElem.textContent = eta !== "---" ? `${eta} (${RESERVE_GALLONS} gal reserve)` : "---";
    }
  } else {
    if (lastFireElem) lastFireElem.textContent = "---";
    if (lastFireEtaElem) lastFireEtaElem.textContent = "---";
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
  const reserveElem = document.getElementById("overview-reserve");
  const vacReadingElem = document.getElementById("vacuum-reading");
  if (reserveElem) reserveElem.textContent = `${RESERVE_GALLONS} gal`;

  if (!summary) {
    if (totalElem) totalElem.textContent = "–";
    if (netFlowElem) netFlowElem.textContent = "–";
    if (overflowTimeElem) overflowTimeElem.textContent = "---";
    if (overflowEtaElem) overflowEtaElem.textContent = "---";
  } else {
    if (totalElem) totalElem.textContent = formatVolumeGal(summary.totalGallons);
    if (netFlowElem) netFlowElem.textContent = formatFlowGph(summary.netFlow);
    if (overflowTimeElem) overflowTimeElem.textContent = formatDurationHhMm(summary.overflowMinutes);
    if (overflowEtaElem) overflowEtaElem.textContent = formatProjectedTime(summary.overflowMinutes, lastGeneratedAt);
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
  const bounds = ensureBounds(pumpYAxisMin, pumpYAxisMax, PUMP_Y_MIN_OPTIONS, PUMP_Y_MAX_OPTIONS);
  pumpYAxisMin = bounds.min;
  pumpYAxisMax = bounds.max;

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
  const yMin = pumpYAxisMin;
  const yMax = pumpYAxisMax;
  ctx.strokeStyle = "rgba(255,255,255,0.12)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  const yTicks = buildTicks(yMin, yMax);
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
  ctx.fillText("gph", 0, 0);
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
    if (note) note.textContent = `Showing last ${FLOW_WINDOWS[flowHistoryWindowSec.toString()] || "window"}`;
    return;
  }

  if (note) {
    note.textContent = `Showing last ${FLOW_WINDOWS[flowHistoryWindowSec.toString()] || "window"}`;
  }
}

function updateEvapHistoryChart() {
  const canvas = document.getElementById("boiling-history-canvas");
  const note = document.getElementById("boiling-history-note");
  if (!canvas) return;

  if (!evapHistory.length) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#1a1f28";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#888";
    ctx.font = "12px sans-serif";
    ctx.fillText("No evaporator data yet", 10, 20);
    if (note) note.textContent = `Showing last ${EVAP_WINDOWS[evapHistoryWindowSec.toString()] || "window"}`;
    return;
  }

  const latestTs = evapHistory[evapHistory.length - 1].t;
  const windowMs = evapHistoryWindowSec * 1000;
  const start = latestTs - windowMs;
  const filtered = evapHistory.filter((pt) => pt.t >= start);
  const bounds = ensureBounds(
    evapPlotSettings.y_axis_min,
    evapPlotSettings.y_axis_max,
    EVAP_Y_MIN_OPTIONS,
    EVAP_Y_MAX_OPTIONS
  );
  evapPlotSettings = {
    ...evapPlotSettings,
    y_axis_min: bounds.min,
    y_axis_max: bounds.max,
  };

  const desiredWidth = canvas.clientWidth || canvas.width || 600;
  if (canvas.width !== desiredWidth) {
    canvas.width = desiredWidth;
  }
  const ctx = canvas.getContext("2d");
  const padLeft = 52;
  const padRight = 10;
  const padTop = 10;
  const padBottom = 30;
  const plotW = canvas.width - padLeft - padRight;
  const plotH = canvas.height - padTop - padBottom;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#1a1f28";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  const yMin = evapPlotSettings.y_axis_min;
  const yMax = evapPlotSettings.y_axis_max;
  ctx.strokeStyle = "rgba(255,255,255,0.12)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  const yTicks = buildTicks(yMin, yMax);
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
  ctx.fillText("gph", 0, 0);
  ctx.restore();

  ctx.textBaseline = "top";
  ctx.textAlign = "center";
  const endLabel = new Date(latestTs);
  const startLabel = new Date(start);
  const fmt = (d) =>
    `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  ctx.fillText(fmt(startLabel), padLeft + 40, canvas.height - padBottom + 6);
  ctx.fillText(fmt(endLabel), canvas.width - padRight - 40, canvas.height - padBottom + 6);
  ctx.fillText(
    `time (last ${EVAP_WINDOWS[evapHistoryWindowSec.toString()] || "window"})`,
    padLeft + plotW / 2,
    canvas.height - padBottom + 6
  );

  // Draw segments colored by draw-off tank
  ctx.lineWidth = 2;
  filtered.forEach((pt, idx) => {
    if (idx === 0) return;
    const prev = filtered[idx - 1];
    const color = DRAW_OFF_COLORS[prev.drawOff] || DRAW_OFF_COLORS["---"];
    ctx.strokeStyle = color;
    ctx.beginPath();
    const x0 = padLeft + ((prev.t - start) / (latestTs - start || 1)) * plotW;
    const y0 = padTop + (1 - (prev.v - yMin) / (yMax - yMin || 1)) * plotH;
    const x1 = padLeft + ((pt.t - start) / (latestTs - start || 1)) * plotW;
    const y1 = padTop + (1 - (pt.v - yMin) / (yMax - yMin || 1)) * plotH;
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
  });

  if (note) note.textContent = `Showing last ${EVAP_WINDOWS[evapHistoryWindowSec.toString()] || "window"}`;
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
  updateBoilingCard(latestEvaporator, overview);
  updateMonitorCard({
    tankSec: tankMonitorSec,
    pumpSec: pumpMonitorSec,
  });
  updatePumpHistoryChart(
    pumpFlowVal != null && pumpTs != null ? { v: pumpFlowVal, t: pumpTs } : null,
    overview?.netFlow != null && netTs != null ? { v: overview.netFlow, t: netTs } : null
  );
  updateEvapHistoryChart();

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
    const [brookside, roadside, pumpRaw, vacuum, monitor, history, evapStatus, evapHistoryResp] = await Promise.all([
      fetchStatusFile(TANK_STATUS_FILES.brookside),
      fetchStatusFile(TANK_STATUS_FILES.roadside),
      fetchStatusFile(PUMP_STATUS_FILE),
      fetchStatusFile(VACUUM_STATUS_FILE),
      fetchStatusFile(MONITOR_STATUS_FILE),
      fetchHistory(),
      fetchStatusFile(EVAP_STATUS_FILE),
      fetchEvaporatorHistory(),
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
    latestEvaporator = evapStatus || latestEvaporator;
    if (evapStatus?.plot_settings) {
      const incoming = coerceEvapSettings(evapStatus.plot_settings);
      if (!evapSettingsPending || settingsEqual(incoming, evapSettingsPending)) {
        evapPlotSettings = incoming;
        if (evapPlotSettings.window_sec) {
          evapHistoryWindowSec = evapPlotSettings.window_sec;
        }
        evapSettingsPending = null;
      }
    }
    applyEvapHistoryResponse(evapHistoryResp);
    if (!latestEvaporator && evapHistoryResp?.latest) {
      latestEvaporator = evapHistoryResp.latest;
    }
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
    syncEvapControls();
    syncPumpControls();
    lastGeneratedAt = computeLatestGenerated([brookside, roadside, pump, vacuum, monitor, latestEvaporator]);
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
  syncPumpControls();
  syncEvapControls();
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

  const pumpMinSel = document.getElementById("pump-y-min");
  const pumpMaxSel = document.getElementById("pump-y-max");
  if (pumpMinSel) {
    pumpMinSel.addEventListener("change", () => {
      const minRaw = Number(pumpMinSel.value);
      const maxRaw = Number(document.getElementById("pump-y-max")?.value);
      const min = Number.isFinite(minRaw) ? minRaw : pumpYAxisMin;
      const max = Number.isFinite(maxRaw) ? maxRaw : pumpYAxisMax;
      const bounds = ensureBounds(min, max, PUMP_Y_MIN_OPTIONS, PUMP_Y_MAX_OPTIONS, false);
      pumpYAxisMin = bounds.min;
      pumpYAxisMax = bounds.max;
      syncPumpControls();
      updatePumpHistoryChart();
    });
  }
  if (pumpMaxSel) {
    pumpMaxSel.addEventListener("change", () => {
      const minRaw = Number(document.getElementById("pump-y-min")?.value);
      const maxRaw = Number(pumpMaxSel.value);
      const min = Number.isFinite(minRaw) ? minRaw : pumpYAxisMin;
      const max = Number.isFinite(maxRaw) ? maxRaw : pumpYAxisMax;
      const bounds = ensureBounds(min, max, PUMP_Y_MIN_OPTIONS, PUMP_Y_MAX_OPTIONS, true);
      pumpYAxisMin = bounds.min;
      pumpYAxisMax = bounds.max;
      syncPumpControls();
      updatePumpHistoryChart();
    });
  }

  const evapMinSel = document.getElementById("boiling-y-min");
  const evapMaxSel = document.getElementById("boiling-y-max");
  const evapWindowSel = document.getElementById("boiling-history-window");

  if (evapMinSel) {
    evapMinSel.addEventListener("change", async () => {
      const minRaw = Number(evapMinSel.value);
      const maxRaw = Number(document.getElementById("boiling-y-max")?.value);
      const min = Number.isFinite(minRaw) ? minRaw : evapPlotSettings.y_axis_min;
      const max = Number.isFinite(maxRaw) ? maxRaw : evapPlotSettings.y_axis_max;
      const bounds = ensureBounds(min, max, EVAP_Y_MIN_OPTIONS, EVAP_Y_MAX_OPTIONS, false);
      evapPlotSettings = {
        ...evapPlotSettings,
        y_axis_min: bounds.min,
        y_axis_max: bounds.max,
      };
      evapSettingsPending = { ...evapPlotSettings, window_sec: evapHistoryWindowSec };
      syncEvapControls();
      updateEvapHistoryChart();
      const saved = await persistEvapSettings({
        ...evapPlotSettings,
        window_sec: evapHistoryWindowSec,
      });
      if (saved?.settings) {
        evapPlotSettings = coerceEvapSettings(saved.settings);
        syncEvapControls();
        if (settingsEqual(evapPlotSettings, saved.settings)) {
          evapSettingsPending = null;
        }
      }
    });
  }
  if (evapMaxSel) {
    evapMaxSel.addEventListener("change", async () => {
      const minRaw = Number(document.getElementById("boiling-y-min")?.value);
      const maxRaw = Number(evapMaxSel.value);
      const min = Number.isFinite(minRaw) ? minRaw : evapPlotSettings.y_axis_min;
      const max = Number.isFinite(maxRaw) ? maxRaw : evapPlotSettings.y_axis_max;
      const bounds = ensureBounds(min, max, EVAP_Y_MIN_OPTIONS, EVAP_Y_MAX_OPTIONS, true);
      evapPlotSettings = {
        ...evapPlotSettings,
        y_axis_min: bounds.min,
        y_axis_max: bounds.max,
      };
      evapSettingsPending = { ...evapPlotSettings, window_sec: evapHistoryWindowSec };
      syncEvapControls();
      updateEvapHistoryChart();
      const saved = await persistEvapSettings({
        ...evapPlotSettings,
        window_sec: evapHistoryWindowSec,
      });
      if (saved?.settings) {
        evapPlotSettings = coerceEvapSettings(saved.settings);
        syncEvapControls();
        if (settingsEqual(evapPlotSettings, saved.settings)) {
          evapSettingsPending = null;
        }
      }
    });
  }
  if (evapWindowSel) {
    evapWindowSel.addEventListener("change", async () => {
      const val = parseInt(evapWindowSel.value, 10);
      if (!Number.isFinite(val)) return;
      evapHistoryWindowSec = val;
      evapPlotSettings = { ...evapPlotSettings, window_sec: val };
      evapSettingsPending = { ...evapPlotSettings };
      syncEvapControls();
      updateEvapHistoryChart();
      const saved = await persistEvapSettings({
        ...evapPlotSettings,
        window_sec: evapHistoryWindowSec,
      });
      if (saved?.settings) {
        evapPlotSettings = coerceEvapSettings(saved.settings);
        syncEvapControls();
        if (settingsEqual(evapPlotSettings, saved.settings)) {
          evapSettingsPending = null;
        }
      }
      const resp = await fetchEvaporatorHistory(val);
      applyEvapHistoryResponse(resp);
      syncEvapControls();
      updateEvapHistoryChart();
      recomputeStalenessAndRender();
    });
  }
}

document.addEventListener("DOMContentLoaded", startLoops);
