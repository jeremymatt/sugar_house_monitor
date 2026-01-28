// web/js/comparison.js

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
const LAMBDA_SYMBOL = "\u03bb";

const LEFT_STREAMS = {
  pump: { label: "Transfer pump flow", color: "#d55e00", source: "flow", valueKey: "flow_gph" },
  inflow: { label: "Tank inflow", color: "#0072b2", source: "flow", valueKey: "flow_gph" },
  evap: { label: "Evaporator flow", color: "#5eb86b", source: "evap", valueKey: "evaporator_flow_gph" },
};

const RIGHT_STREAMS = {
  vacuum: {
    label: "Vacuum (inHg)",
    color: "#cfd2d9",
    source: "flow",
    valueKey: "reading_inhg",
    transform: (val) => -val,
  },
  o2: {
    label: `O2 (${LAMBDA_SYMBOL})`,
    color: "#f2a93b",
    source: "flow",
    valueKey: "o2_percent",
  },
  stack: { label: "Stack temp (F)", color: "#cfd2d9", source: "evap", valueKey: "stack_temp_f" },
};

const SECONDARY_DASH = [6, 4];
const X_TICK_COUNT = 6;
const Y_TICK_COUNT = 5;

let refreshTimer = null;
let refreshToken = 0;

function toNumber(value) {
  if (value == null || value === "" || !isFinite(value)) return null;
  return Number(value);
}

function pad2(val) {
  return String(val).padStart(2, "0");
}

function toLocalIso(dt) {
  const year = dt.getFullYear();
  const month = pad2(dt.getMonth() + 1);
  const day = pad2(dt.getDate());
  const hour = pad2(dt.getHours());
  const minute = pad2(dt.getMinutes());
  const offsetMin = -dt.getTimezoneOffset();
  const sign = offsetMin >= 0 ? "+" : "-";
  const abs = Math.abs(offsetMin);
  const offsetH = pad2(Math.floor(abs / 60));
  const offsetM = pad2(abs % 60);
  return `${year}-${month}-${day}T${hour}:${minute}:00${sign}${offsetH}:${offsetM}`;
}

function daysInMonth(year, month) {
  return new Date(year, month, 0).getDate();
}

function buildSelectOptions(select, values, format) {
  select.innerHTML = "";
  values.forEach((val) => {
    const opt = document.createElement("option");
    opt.value = String(val);
    opt.textContent = format ? format(val) : String(val);
    select.appendChild(opt);
  });
}

function initDateControls(container, defaultDate) {
  const monthSel = container.querySelector('[data-part="month"]');
  const daySel = container.querySelector('[data-part="day"]');
  const yearSel = container.querySelector('[data-part="year"]');
  const hourSel = container.querySelector('[data-part="hour"]');
  const minuteSel = container.querySelector('[data-part="minute"]');

  function updateDays() {
    const year = parseInt(yearSel.value, 10);
    const month = parseInt(monthSel.value, 10);
    const maxDay = daysInMonth(year, month);
    const current = parseInt(daySel.value || "1", 10);
    buildSelectOptions(daySel, Array.from({ length: maxDay }, (_, i) => i + 1), pad2);
    daySel.value = String(Math.min(current, maxDay));
  }

  function setValues(date) {
    yearSel.value = String(date.getFullYear());
    monthSel.value = String(date.getMonth() + 1);
    updateDays();
    daySel.value = String(date.getDate());
    hourSel.value = String(date.getHours());
    minuteSel.value = String(date.getMinutes());
  }

  if (container.dataset.initialized) {
    setValues(defaultDate);
    return;
  }

  const now = new Date();
  const yearStart = now.getFullYear() - 5;
  const yearEnd = now.getFullYear() + 1;
  const years = [];
  for (let y = yearStart; y <= yearEnd; y += 1) years.push(y);
  buildSelectOptions(yearSel, years);
  buildSelectOptions(monthSel, Array.from({ length: 12 }, (_, i) => i + 1), pad2);
  buildSelectOptions(hourSel, Array.from({ length: 24 }, (_, i) => i), pad2);
  buildSelectOptions(minuteSel, Array.from({ length: 12 }, (_, i) => i * 5), pad2);

  yearSel.addEventListener("change", updateDays);
  monthSel.addEventListener("change", updateDays);
  container.dataset.initialized = "1";
  setValues(defaultDate);
}

function getSelectedDate(container) {
  const year = parseInt(container.querySelector('[data-part="year"]').value, 10);
  const month = parseInt(container.querySelector('[data-part="month"]').value, 10);
  const day = parseInt(container.querySelector('[data-part="day"]').value, 10);
  const hour = parseInt(container.querySelector('[data-part="hour"]').value, 10);
  const minute = parseInt(container.querySelector('[data-part="minute"]').value, 10);
  return new Date(year, month - 1, day, hour, minute, 0, 0);
}

function windowSeconds() {
  const rawValue = toNumber(document.getElementById("window-value").value);
  const unit = document.getElementById("window-unit").value;
  const val = rawValue != null && rawValue > 0 ? rawValue : 6;
  const factor = unit === "minutes" ? 60 : unit === "days" ? 86400 : 3600;
  return { seconds: Math.round(val * factor), unit, unitValue: val };
}

function buildQuery(startIso, windowSec, numBins) {
  const params = new URLSearchParams();
  params.set("start_ts", startIso);
  params.set("window_sec", String(windowSec));
  if (numBins) {
    params.set("num_bins", String(numBins));
  }
  return params.toString();
}

async function fetchJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function normalizeSeries(rows, valueKey, startMs, windowSec, transform) {
  if (!Array.isArray(rows)) return [];
  const output = [];
  rows.forEach((row) => {
    const ts = row.ts || row.source_timestamp || row.sample_timestamp;
    if (!ts) return;
    const ms = Date.parse(ts);
    if (!isFinite(ms)) return;
    const tSec = (ms - startMs) / 1000;
    if (tSec < 0 || tSec > windowSec) return;
    const valueRaw = toNumber(row[valueKey]);
    const value = valueRaw == null ? null : transform ? transform(valueRaw) : valueRaw;
    if (value == null) return;
    output.push({ t: tSec, v: value });
  });
  return output;
}

function computeBounds(seriesList) {
  let min = null;
  let max = null;
  seriesList.forEach((series) => {
    series.forEach((pt) => {
      if (min === null || pt.v < min) min = pt.v;
      if (max === null || pt.v > max) max = pt.v;
    });
  });
  if (min === null || max === null) return null;
  if (min === max) {
    return { min: min - 1, max: max + 1 };
  }
  const pad = (max - min) * 0.05;
  return { min: min - pad, max: max + pad };
}

function buildTicks(min, max, count) {
  const ticks = [];
  if (count <= 1) return ticks;
  const step = (max - min) / (count - 1);
  for (let i = 0; i < count; i += 1) {
    ticks.push(min + step * i);
  }
  return ticks;
}

function tickDecimals(range) {
  const abs = Math.abs(range);
  if (!isFinite(abs) || abs === 0) return 0;
  if (abs <= 2) return 2;
  if (abs <= 10) return 1;
  return 0;
}

function formatTick(value, range) {
  const decimals = tickDecimals(range);
  const text = value.toFixed(decimals);
  return text.replace(/^-0(\.0+)?$/, "0");
}

function resizeCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const desiredWidth = Math.max(200, Math.floor(rect.width));
  const desiredHeight = Math.max(200, Math.floor(desiredWidth * 9 / 16));
  if (canvas.width !== desiredWidth) canvas.width = desiredWidth;
  if (canvas.height !== desiredHeight) canvas.height = desiredHeight;
}

function drawLine(ctx, series, color, xMax, yMin, yMax, dims, options = {}) {
  if (!series.length) return;
  const range = yMax - yMin || 1;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = options.lineWidth || 2;
  if (options.dash) ctx.setLineDash(options.dash);
  ctx.beginPath();
  series.forEach((pt, idx) => {
    const x = dims.padLeft + (pt.t / xMax) * dims.plotW;
    const y = dims.padTop + (1 - (pt.v - yMin) / range) * dims.plotH;
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.restore();
}

function drawPlot(canvas, payload) {
  const ctx = canvas.getContext("2d");
  resizeCanvas(canvas);
  const { width, height } = canvas;
  const padLeft = 70;
  const padRight = payload.rightBounds ? 70 : 18;
  const padTop = 20;
  const padBottom = 42;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;
  const fontTick = "18px system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif";
  const fontLabel = "20px system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif";
  const leftLabel = payload.leftLabel || "gph";

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#1a1f28";
  ctx.fillRect(0, 0, width, height);

  if (!payload.leftBounds) {
    ctx.fillStyle = "#888";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = fontLabel;
    ctx.fillText("No data selected", width / 2, height / 2);
    return;
  }

  const leftTicks = buildTicks(payload.leftBounds.min, payload.leftBounds.max, Y_TICK_COUNT);
  const leftRange = payload.leftBounds.max - payload.leftBounds.min;
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 1;
  leftTicks.forEach((val, idx) => {
    const frac = (val - payload.leftBounds.min) / (payload.leftBounds.max - payload.leftBounds.min || 1);
    const y = padTop + plotH - frac * plotH;
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(width - padRight, y);
    ctx.stroke();
    if (idx > 0) {
      ctx.fillStyle = "#a7afbf";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.font = fontTick;
      ctx.fillText(formatTick(val, leftRange), padLeft - 8, y);
    }
  });

  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.beginPath();
  ctx.moveTo(padLeft, padTop);
  ctx.lineTo(padLeft, padTop + plotH);
  ctx.lineTo(width - padRight, padTop + plotH);
  ctx.stroke();

  if (payload.rightBounds) {
    const rightTicks = buildTicks(payload.rightBounds.min, payload.rightBounds.max, Y_TICK_COUNT);
    const rightRange = payload.rightBounds.max - payload.rightBounds.min;
    rightTicks.forEach((val) => {
      const frac = (val - payload.rightBounds.min) / (payload.rightBounds.max - payload.rightBounds.min || 1);
      const y = padTop + plotH - frac * plotH;
      ctx.fillStyle = "#a7afbf";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.font = fontTick;
      ctx.fillText(formatTick(val, rightRange), width - padRight + 8, y);
    });

    if (payload.rightLabel) {
      ctx.save();
      ctx.translate(width - 10, padTop + plotH / 2);
      ctx.rotate(Math.PI / 2);
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = "#cfd2d9";
      ctx.font = fontLabel;
      ctx.fillText(payload.rightLabel, 0, 0);
      ctx.restore();
    }
  }

  const xMax = payload.windowSec;
  const unitSec = payload.unit === "minutes" ? 60 : payload.unit === "days" ? 86400 : 3600;
  const xMaxUnits = xMax / unitSec;
  const xTicks = buildTicks(0, xMaxUnits, X_TICK_COUNT);
  const xRange = xMaxUnits;
  ctx.fillStyle = "#a7afbf";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  xTicks.forEach((val) => {
    const x = padLeft + (val / xMaxUnits) * plotW;
    ctx.beginPath();
    ctx.moveTo(x, padTop + plotH);
    ctx.lineTo(x, padTop + plotH + 4);
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, padTop + plotH);
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.stroke();
    ctx.font = fontTick;
    ctx.fillText(formatTick(val, xRange), x, padTop + plotH + 6);
  });
  ctx.font = fontLabel;
  ctx.fillText(`elapsed ${payload.unit}`, padLeft + plotW / 2, height - 20);
  ctx.save();
  ctx.translate(10, padTop + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillStyle = "#a7afbf";
  ctx.font = fontLabel;
  ctx.fillText(leftLabel, 0, 0);
  ctx.restore();

  const dims = { padLeft, padTop, plotW, plotH };
  payload.series.forEach((series) => {
    drawLine(ctx, series.points, series.color, xMax, payload.leftBounds.min, payload.leftBounds.max, dims, series.style);
  });
  if (payload.rightBounds) {
    payload.rightSeries.forEach((series) => {
      drawLine(ctx, series.points, series.color, xMax, payload.rightBounds.min, payload.rightBounds.max, dims, series.style);
    });
  }
}

function updateLegend(series, rightSeries) {
  const legend = document.getElementById("comparison-legend");
  legend.innerHTML = "";
  const addItem = (label, color, dashed) => {
    const item = document.createElement("div");
    item.className = "legend-item";
    item.style.color = color;
    const line = document.createElement("span");
    line.className = `legend-line${dashed ? " dashed" : ""}`;
    item.appendChild(line);
    const text = document.createElement("span");
    text.textContent = label;
    item.appendChild(text);
    legend.appendChild(item);
  };
  series.forEach((entry) => addItem(entry.label, entry.color, entry.dashed));
  rightSeries.forEach((entry) => addItem(entry.label, entry.color, entry.dashed));
}

function gatherSelections() {
  const leftStreams = [];
  if (document.getElementById("stream-pump").checked) leftStreams.push("pump");
  if (document.getElementById("stream-inflow").checked) leftStreams.push("inflow");
  if (document.getElementById("stream-evap").checked) leftStreams.push("evap");
  const rightAxis = document.getElementById("right-axis").value;
  const secondaryEnabled = document.getElementById("secondary-enabled").checked;
  const numBins = toNumber(document.getElementById("num-bins").value);
  return { leftStreams, rightAxis, secondaryEnabled, numBins };
}

async function loadWindowData(startIso, windowSec, numBins, selections) {
  const wantsFlow = selections.leftStreams.some((s) => LEFT_STREAMS[s].source === "flow") ||
    selections.rightAxis === "vacuum" ||
    selections.rightAxis === "o2";
  const wantsEvap = selections.leftStreams.some((s) => LEFT_STREAMS[s].source === "evap") ||
    selections.rightAxis === "stack";

  const params = buildQuery(startIso, windowSec, numBins);
  const results = {};
  if (wantsFlow) {
    results.flow = await fetchJson(`${FLOW_HISTORY_ENDPOINT}?${params}`);
  }
  if (wantsEvap) {
    results.evap = await fetchJson(`${EVAP_HISTORY_ENDPOINT}?${params}`);
  }
  return results;
}

function normalizeWindowSeries(startIso, windowSec, data, selections) {
  const startMs = Date.parse(data?.flow?.start_ts_used || data?.evap?.start_ts_used || startIso);
  const leftSeries = [];
  selections.leftStreams.forEach((streamKey) => {
    const cfg = LEFT_STREAMS[streamKey];
    const source = data[cfg.source];
    if (!source) return;
    const rows = streamKey === "evap" ? source.history : source[streamKey];
    const points = normalizeSeries(rows, cfg.valueKey, startMs, windowSec);
    leftSeries.push({ key: streamKey, label: cfg.label, color: cfg.color, points });
  });

  let rightSeries = [];
  if (selections.rightAxis && selections.rightAxis !== "none") {
    const cfg = RIGHT_STREAMS[selections.rightAxis];
    const source = data[cfg.source];
    let rows = null;
    if (selections.rightAxis === "stack") {
      rows = source?.stack_history;
    } else if (selections.rightAxis === "o2") {
      rows = source?.o2;
    } else {
      rows = source?.vacuum;
    }
    const points = normalizeSeries(rows, cfg.valueKey, startMs, windowSec, cfg.transform);
    rightSeries = [{ key: selections.rightAxis, label: cfg.label, color: cfg.color, points }];
  }

  return { leftSeries, rightSeries, numBinsUsed: data.flow?.num_bins_used || data.evap?.num_bins_used || null };
}

async function refreshPlot() {
  const token = ++refreshToken;
  const { seconds: windowSec, unit, unitValue } = windowSeconds();
  const primaryStart = getSelectedDate(document.getElementById("primary-start"));
  const secondaryStart = getSelectedDate(document.getElementById("secondary-start"));
  const selections = gatherSelections();

  const primaryIso = toLocalIso(primaryStart);
  const secondaryIso = toLocalIso(secondaryStart);

  const note = document.getElementById("comparison-note");
  if (note) {
    note.textContent = "";
  }

  try {
    const primaryData = await loadWindowData(primaryIso, windowSec, selections.numBins, selections);
    if (token !== refreshToken) return;
    const primary = normalizeWindowSeries(primaryIso, windowSec, primaryData, selections);

    let secondary = null;
    if (selections.secondaryEnabled) {
      const secondaryData = await loadWindowData(secondaryIso, windowSec, selections.numBins, selections);
      if (token !== refreshToken) return;
      secondary = normalizeWindowSeries(secondaryIso, windowSec, secondaryData, selections);
    }

    const leftSeries = [];
    const rightSeries = [];
    const legendSeries = [];
    const legendRight = [];
    primary.leftSeries.forEach((series) => {
      leftSeries.push({
        points: series.points,
        color: series.color,
        style: { lineWidth: 2 },
      });
      legendSeries.push({ label: `${series.label} (primary)`, color: series.color, dashed: false });
    });
    if (secondary) {
      secondary.leftSeries.forEach((series) => {
        leftSeries.push({
          points: series.points,
          color: series.color,
          style: { lineWidth: 2, dash: SECONDARY_DASH },
        });
        legendSeries.push({ label: `${series.label} (secondary)`, color: series.color, dashed: true });
      });
    }

    if (primary.rightSeries.length) {
      primary.rightSeries.forEach((series) => {
        rightSeries.push({ points: series.points, color: series.color, style: { lineWidth: 2 } });
        legendRight.push({ label: `${series.label} (primary)`, color: series.color, dashed: false });
      });
    }
    if (secondary && secondary.rightSeries.length) {
      secondary.rightSeries.forEach((series) => {
        rightSeries.push({ points: series.points, color: series.color, style: { lineWidth: 2, dash: SECONDARY_DASH } });
        legendRight.push({ label: `${series.label} (secondary)`, color: series.color, dashed: true });
      });
    }

    let leftBounds = computeBounds(leftSeries.map((s) => s.points));
    let rightBounds = rightSeries.length ? computeBounds(rightSeries.map((s) => s.points)) : null;
    let leftLabel = "gph";
    let rightLabel = "";
    if (rightBounds && selections.rightAxis === "vacuum") rightLabel = "inHg";
    if (rightBounds && selections.rightAxis === "o2") rightLabel = LAMBDA_SYMBOL;
    if (rightBounds && selections.rightAxis === "stack") rightLabel = "F";

    if (!leftBounds && rightBounds) {
      leftSeries.push(...rightSeries);
      legendSeries.push(...legendRight);
      rightSeries.length = 0;
      legendRight.length = 0;
      leftBounds = rightBounds;
      leftLabel = rightLabel || leftLabel;
      rightBounds = null;
      rightLabel = "";
    }

    updateLegend(legendSeries, legendRight);

    const canvas = document.getElementById("comparison-canvas");
    drawPlot(canvas, {
      windowSec,
      unit,
      leftBounds,
      leftLabel,
      rightBounds,
      rightLabel,
      series: leftSeries,
      rightSeries,
    });

    const binsUsed = primary.numBinsUsed || secondary?.numBinsUsed || "--";
    const binsElem = document.getElementById("num-bins-used");
    if (binsElem) binsElem.textContent = binsUsed;
  } catch (err) {
    console.warn("Comparison plot refresh failed:", err);
    const canvas = document.getElementById("comparison-canvas");
    const ctx = canvas.getContext("2d");
    resizeCanvas(canvas);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#1a1f28";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#888";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("Error loading comparison data", canvas.width / 2, canvas.height / 2);
  }
}

function scheduleRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refreshPlot, 200);
}

function defaultStartTime() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate(), 8, 0, 0, 0);
}

function resetControls() {
  document.getElementById("window-value").value = "6";
  document.getElementById("window-unit").value = "hours";
  document.getElementById("right-axis").value = "none";
  document.getElementById("num-bins").value = "";
  document.getElementById("stream-pump").checked = true;
  document.getElementById("stream-inflow").checked = true;
  document.getElementById("stream-evap").checked = true;
  document.getElementById("secondary-enabled").checked = false;
  document.getElementById("secondary-start").style.display = "none";
  const start = defaultStartTime();
  initDateControls(document.getElementById("primary-start"), start);
  initDateControls(document.getElementById("secondary-start"), start);
}

function attachListeners() {
  document.querySelectorAll("input, select").forEach((elem) => {
    elem.addEventListener("change", () => {
      if (elem.id === "secondary-enabled") {
        const secondary = document.getElementById("secondary-start");
        secondary.style.display = elem.checked ? "flex" : "none";
      }
      scheduleRefresh();
    });
  });
  document.getElementById("reset-btn").addEventListener("click", () => {
    resetControls();
    scheduleRefresh();
  });
  const refreshBtn = document.getElementById("refresh-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      scheduleRefresh();
    });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  resetControls();
  attachListeners();
  scheduleRefresh();
});
