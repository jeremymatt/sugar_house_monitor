#!/usr/bin/env python3
"""
Lightweight fullscreen display for Pi Zero W showing evaporator flow history.

- Fetches latest status + history from the WordPress server (status_evaporator.json
  and evaporator_history.php).
- Uses saved plot_settings (window/y-limits) from the server; defaults to 2h, 200–600.
- Redraws every 15s. No mouse/UI; autohides cursor and disables screen blanking.
"""
from __future__ import annotations

import os
import sys
import time
import math
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import pygame
import requests


# ---- CONFIG ----
DEBUG = os.environ.get("DISPLAY_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}

# Base API endpoint (ending in /api)
API_BASE = os.environ.get(
    "DISPLAY_API_BASE", "https://mattsmaplesyrup.com/sugar_house_monitor/api"
).rstrip("/")

# Derive data base from API base unless explicitly provided.
BASE_URL = os.environ.get("DISPLAY_BASE_URL")
if not BASE_URL:
    BASE_URL = API_BASE.rsplit("/api", 1)[0]

STATUS_URL = os.environ.get(
    "DISPLAY_STATUS_URL", f"{BASE_URL}/data/status_evaporator.json"
)
STACK_STATUS_URL = os.environ.get(
    "DISPLAY_STACK_STATUS_URL", f"{BASE_URL}/data/status_stack.json"
)
O2_STATUS_URL = os.environ.get(
    "DISPLAY_O2_STATUS_URL", f"{BASE_URL}/data/status_o2.json"
)
HISTORY_URL = os.environ.get(
    "DISPLAY_HISTORY_URL", f"{API_BASE}/evaporator_history.php"
)
REFRESH_SEC = float(os.environ.get("DISPLAY_REFRESH_SEC", "15"))
SNAPSHOT_PATH = os.environ.get("DISPLAY_SNAPSHOT_PATH", "").strip() or "~/display_state.png"
SNAPSHOT_PATH = os.path.expanduser(SNAPSHOT_PATH)
SNAPSHOT_INTERVAL_RAW = os.environ.get("DISPLAY_SNAPSHOT_INTERVAL_SEC", "").strip()
if SNAPSHOT_INTERVAL_RAW:
    try:
        SNAPSHOT_INTERVAL_SEC = float(SNAPSHOT_INTERVAL_RAW)
    except ValueError:
        SNAPSHOT_INTERVAL_SEC = REFRESH_SEC
else:
    SNAPSHOT_INTERVAL_SEC = REFRESH_SEC
if SNAPSHOT_INTERVAL_SEC <= 0:
    SNAPSHOT_INTERVAL_SEC = None
HISTORY_SCOPE = os.environ.get("DISPLAY_HISTORY_SCOPE", "display").strip().lower() or "display"
WINDOW_OVERRIDE_SEC = os.environ.get("DISPLAY_WINDOW_OVERRIDE_SEC", "").strip()
WINDOW_OVERRIDE = None
if WINDOW_OVERRIDE_SEC:
    try:
        parsed = int(WINDOW_OVERRIDE_SEC)
        if parsed > 0:
            WINDOW_OVERRIDE = parsed
    except ValueError:
        WINDOW_OVERRIDE = None
RAW_PLOT_BINS = os.environ.get("DISPLAY_NUM_PLOT_BINS") or os.environ.get("NUM_PLOT_BINS")
PLOT_BINS = None
if RAW_PLOT_BINS:
    try:
        parsed = int(RAW_PLOT_BINS)
        if parsed > 0:
            PLOT_BINS = parsed
    except ValueError:
        PLOT_BINS = None
WINDOW_DEFAULT = 2 * 60 * 60  # 2h
YMIN_DEFAULT = 0.0
YMAX_DEFAULT = 600.0
TANKS_EMPTYING_THRESHOLD = -10.0
RESERVE_GALLONS = 150.0

SCREEN_WIDTH = 800
SCREEN_HEIGHT = 480

FONT_SCALE = 2
UI_SCALE = 2

BACKGROUND = (255, 255, 255)
CARD_BG = (245, 245, 245)
TEXT_MAIN = (0, 0, 0)
TEXT_MUTED = (45, 45, 45)
AXIS_GRID = (205, 205, 205)
AXIS_LABEL = (30, 30, 30)
COLORS = {
    "---": (120, 120, 120),      # gray
    "brookside": (0, 90, 140),   # blue (darker)
    "roadside": (180, 70, 0),    # orange (darker)
}
STACK_LINE = (90, 90, 90)  # dark gray dashed line for stack temp
LAMBDA_SYMBOL = "\u03bb"

try:
    import pygame.freetype as pg_ft
    HAS_FREETYPE = True
except Exception:
    HAS_FREETYPE = False
try:
    import pygame.font as pg_font
    HAS_FONT = True
except Exception:
    HAS_FONT = False

# Cache fonts so we do not recreate them every frame.
_FONT_CACHE = {}
_WARN_NO_FONT = False
_WARN_RENDER_FAIL = False


def debug_log(message: str) -> None:
    if DEBUG:
        print(f"[display debug] {message}", file=sys.stderr, flush=True)


def save_snapshot(surface, path: str) -> None:
    if not path:
        return
    base, ext = os.path.splitext(path)
    if not ext:
        ext = ".png"
        path = f"{base}{ext}"
    tmp_path = f"{base}.tmp{ext}"
    try:
        dir_path = os.path.dirname(path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        pygame.image.save(surface, tmp_path)
        os.replace(tmp_path, path)
    except Exception as exc:
        debug_log(f"Snapshot save failed: {exc}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


@dataclass
class PlotSettings:
    y_min: float = YMIN_DEFAULT
    y_max: float = YMAX_DEFAULT
    window_sec: int = WINDOW_DEFAULT


@dataclass
class EvapPoint:
    t_ms: int
    flow: float
    draw_off: str


@dataclass
class StackPoint:
    t_ms: int
    temp_f: float


@dataclass
class EvapStatus:
    sample_ts: Optional[str]
    draw_off: str
    draw_off_flow: Optional[float]
    pump_in: str
    pump_in_flow: Optional[float]
    evap_flow: Optional[float]
    last_fire_min: Optional[float] = None
    stack_temp_f: Optional[float] = None
    o2_lambda: Optional[float] = None


# ---- DATA FETCHING ----


def fetch_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code} for {url}", file=sys.stderr)
            return None
        text = resp.text.strip()
        if not text:
            return None
        return json.loads(text)
    except Exception as exc:
        print(f"Fetch failed for {url}: {exc}", file=sys.stderr)
        return None


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def ms_from_iso(ts: Optional[str]) -> Optional[int]:
    dt = parse_iso(ts)
    return int(dt.timestamp() * 1000) if dt else None


def fetch_state() -> Tuple[PlotSettings, List[EvapPoint], List[StackPoint], EvapStatus]:
    cache_bust = int(time.time())
    status_payload = fetch_json(STATUS_URL, params={"t": cache_bust}) or {}
    stack_payload = fetch_json(STACK_STATUS_URL, params={"t": cache_bust}) or {}
    o2_payload = fetch_json(O2_STATUS_URL, params={"t": cache_bust}) or {}
    brook_status = fetch_json(f"{BASE_URL}/data/status_brookside.json", params={"t": cache_bust}) or {}
    road_status = fetch_json(f"{BASE_URL}/data/status_roadside.json", params={"t": cache_bust}) or {}
    history_params = {"t": cache_bust, "scope": HISTORY_SCOPE}
    if WINDOW_OVERRIDE is not None:
        history_params["window_sec"] = WINDOW_OVERRIDE
    if PLOT_BINS:
        history_params["num_bins"] = PLOT_BINS
    history_payload = fetch_json(HISTORY_URL, params=history_params) or {}

    # Settings
    settings_raw = history_payload.get("settings") or {}
    y_min_val = settings_raw.get("y_axis_min")
    y_max_val = settings_raw.get("y_axis_max")
    win_val = settings_raw.get("window_sec")
    settings = PlotSettings(
        y_min=float(y_min_val) if y_min_val is not None else YMIN_DEFAULT,
        y_max=float(y_max_val) if y_max_val is not None else YMAX_DEFAULT,
        window_sec=int(win_val) if win_val is not None else WINDOW_DEFAULT,
    )

    # History
    points: List[EvapPoint] = []
    stack_points: List[StackPoint] = []
    for row in history_payload.get("history") or []:
        t_ms = ms_from_iso(row.get("ts"))
        flow = row.get("evaporator_flow_gph")
        draw_off = (row.get("draw_off_tank") or "---").lower()
        if t_ms is None or flow is None:
            continue
        try:
            f = float(flow)
        except (TypeError, ValueError):
            continue
        points.append(EvapPoint(t_ms=t_ms, flow=f, draw_off=draw_off))

    stack_points: List[StackPoint] = []
    for row in history_payload.get("stack_history") or []:
        t_ms = ms_from_iso(row.get("ts"))
        temp = row.get("stack_temp_f")
        if t_ms is None or temp is None:
            continue
        try:
            val = float(temp)
        except (TypeError, ValueError):
            continue
        stack_points.append(StackPoint(t_ms=t_ms, temp_f=val))

    # Latest status
    latest = status_payload or {}
    last_fire_min = None
    try:
        b_flow = float(brook_status.get("flow_gph")) if brook_status.get("flow_gph") is not None else None
        r_flow = float(road_status.get("flow_gph")) if road_status.get("flow_gph") is not None else None
        b_vol = float(brook_status.get("volume_gal")) if brook_status.get("volume_gal") is not None else 0.0
        r_vol = float(road_status.get("volume_gal")) if road_status.get("volume_gal") is not None else 0.0
        total = b_vol + r_vol
        net_flow = (b_flow or 0.0) + (r_flow or 0.0)
        if net_flow <= TANKS_EMPTYING_THRESHOLD:
            available = max(total - RESERVE_GALLONS, 0.0)
            if abs(net_flow) > 0:
                last_fire_min = (available / abs(net_flow)) * 60.0
    except Exception:
        last_fire_min = None

    stack_temp = None
    try:
        if stack_payload.get("stack_temp_f") is not None:
            stack_temp = float(stack_payload.get("stack_temp_f"))
    except (TypeError, ValueError):
        stack_temp = None

    o2_lambda = None
    try:
        if o2_payload.get("o2_percent") is not None:
            o2_lambda = float(o2_payload.get("o2_percent"))
    except (TypeError, ValueError):
        o2_lambda = None

    status = EvapStatus(
        sample_ts=latest.get("sample_timestamp"),
        draw_off=(latest.get("draw_off_tank") or "---").lower(),
        draw_off_flow=latest.get("draw_off_flow_gph"),
        pump_in=(latest.get("pump_in_tank") or "---").lower(),
        pump_in_flow=latest.get("pump_in_flow_gph"),
        evap_flow=latest.get("evaporator_flow_gph"),
        last_fire_min=last_fire_min,
        stack_temp_f=stack_temp,
        o2_lambda=o2_lambda,
    )

    return settings, points, stack_points, status


# ---- RENDERING ----


def scale_font(size: int) -> int:
    return int(round(size * FONT_SCALE))


def scale_ui(value: float) -> int:
    return int(round(value * UI_SCALE))


def get_font(size: int, bold: bool):
    scaled_size = scale_font(size)
    key = (scaled_size, bool(bold))
    font_obj = _FONT_CACHE.get(key)
    if font_obj:
        return font_obj

    if not font_obj and HAS_FONT:
        try:
            pg_font.init()
            font_obj = pg_font.SysFont(None, scaled_size, bold=bold) or pg_font.Font(None, scaled_size)
        except Exception as exc:
            debug_log(f"SysFont failed for size={scaled_size} bold={bold}: {exc}")
            try:
                font_obj = pg_font.Font(None, scaled_size)
            except Exception as exc_font:
                debug_log(f"Font(None) failed for size={scaled_size}: {exc_font}")
                font_obj = None

    if not font_obj and HAS_FREETYPE:
        try:
            pg_ft.init()
            font_obj = pg_ft.SysFont(None, scaled_size, bold=bold)
        except Exception as exc:
            debug_log(f"FreeType SysFont failed for size={scaled_size} bold={bold}: {exc}")
            font_obj = None

    if font_obj:
        _FONT_CACHE[key] = font_obj

    return font_obj


def measure_text(text: str, size=20, bold=False) -> int:
    font_obj = get_font(size, bold)
    if not font_obj:
        return 0
    try:
        if hasattr(font_obj, "get_rect"):
            return font_obj.get_rect(text).width
        if hasattr(font_obj, "size"):
            return font_obj.size(text)[0]
    except Exception:
        return 0
    return 0


def draw_text(surface, text, pos, size=20, color=TEXT_MAIN, bold=False):
    global _WARN_NO_FONT, _WARN_RENDER_FAIL
    font_obj = get_font(size, bold)
    if not font_obj:
        if not _WARN_NO_FONT:
            debug_log("No usable font available; text will not render.")
            _WARN_NO_FONT = True
        return

    try:
        if hasattr(font_obj, "render_to"):
            font_obj.render_to(surface, pos, text, color)
        else:
            render = font_obj.render(text, True, color)
            surface.blit(render, pos)
    except Exception as exc:
        if not _WARN_RENDER_FAIL:
            debug_log(f"Font render failed for text '{text}' at {pos}: {exc}")
            _WARN_RENDER_FAIL = True
        return


def auto_bounds(values: List[float]) -> Optional[Tuple[float, float]]:
    if not values:
        return None
    min_val = min(values)
    max_val = max(values)
    if min_val == max_val:
        min_val -= 1
        max_val += 1
    pad = max(2.0, (max_val - min_val) * 0.1)
    return min_val - pad, max_val + pad


def draw_dashed_line(surface, color, start, end, dash_len, gap_len, width):
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dist = math.hypot(dx, dy)
    if dist <= 0:
        return
    dash_len = max(1, int(dash_len))
    gap_len = max(1, int(gap_len))
    step = dash_len + gap_len
    progress = 0.0
    while progress < dist:
        seg_start = progress
        seg_end = min(progress + dash_len, dist)
        x1 = start[0] + (dx * (seg_start / dist))
        y1 = start[1] + (dy * (seg_start / dist))
        x2 = start[0] + (dx * (seg_end / dist))
        y2 = start[1] + (dy * (seg_end / dist))
        pygame.draw.line(surface, color, (x1, y1), (x2, y2), width)
        progress += step


def draw_dashed_polyline(surface, color, points, dash_len, gap_len, width):
    for idx in range(1, len(points)):
        draw_dashed_line(surface, color, points[idx - 1], points[idx], dash_len, gap_len, width)


def draw_chart(
    surface,
    rect,
    settings: PlotSettings,
    points: List[EvapPoint],
    stack_points: List[StackPoint],
):
    pygame.draw.rect(surface, CARD_BG, rect, border_radius=scale_ui(12))
    y_min = settings.y_min
    y_max = settings.y_max if settings.y_max > y_min else y_min + 1
    window_ms = settings.window_sec * 1000
    if not points and not stack_points:
        draw_text(
            surface,
            "No evaporator data",
            (rect.x + scale_ui(10), rect.y + scale_ui(10)),
            size=18,
            color=TEXT_MUTED,
        )
        return

    latest_t = max(
        points[-1].t_ms if points else 0,
        stack_points[-1].t_ms if stack_points else 0,
    )
    if latest_t <= 0:
        draw_text(
            surface,
            "No evaporator data",
            (rect.x + scale_ui(10), rect.y + scale_ui(10)),
            size=18,
            color=TEXT_MUTED,
        )
        return
    start_t = latest_t - window_ms
    pts = [p for p in points if p.t_ms >= start_t]
    stack_pts = [p for p in stack_points if p.t_ms >= start_t]
    if points and not pts:
        pts = points[-100:]  # fallback
        start_t = pts[0].t_ms
        latest_t = pts[-1].t_ms
        stack_pts = [p for p in stack_points if p.t_ms >= start_t]

    pad_left = scale_ui(45)
    pad_right = scale_ui(40)
    pad_top = scale_ui(12)
    pad_bottom = scale_ui(30)
    plot_w = rect.width - pad_left - pad_right
    plot_h = rect.height - pad_top - pad_bottom

    stack_bounds = auto_bounds([p.temp_f for p in stack_pts])

    tick_size = 14
    tick_gap = scale_ui(2)
    tick_height = scale_font(tick_size) + tick_gap
    grid_width = max(1, scale_ui(1))

    # Grid + ticks
    x_ticks = 5
    x_label_y = rect.y + pad_top + plot_h + scale_ui(0)
    for i in range(x_ticks + 1):
        x = rect.x + pad_left + (i / x_ticks) * plot_w
        pygame.draw.line(
            surface,
            AXIS_GRID,
            (x, rect.y + pad_top),
            (x, rect.y + pad_top + plot_h),
            grid_width,
        )
        t_tick = start_t + (i / x_ticks) * (latest_t - start_t)
        tick_dt = datetime.fromtimestamp(t_tick / 1000, tz=timezone.utc)
        tick_label = tick_dt.strftime("%H:%M")
        draw_text(surface, tick_label, (x - scale_ui(18), x_label_y), size=tick_size, color=AXIS_LABEL)

    y_ticks = 5
    for i in range(y_ticks + 1):
        y_val = y_min + (i / y_ticks) * (y_max - y_min)
        y = rect.y + pad_top + plot_h - (i / y_ticks) * plot_h
        pygame.draw.line(
            surface,
            AXIS_GRID,
            (rect.x + pad_left, y),
            (rect.x + pad_left + plot_w, y),
            grid_width,
        )
        draw_text(
            surface,
            f"{y_val:.0f}",
            (rect.x + pad_left - scale_ui(28), y - scale_ui(8)),
            size=tick_size,
            color=AXIS_LABEL,
        )

    if stack_bounds:
        stack_min, stack_max = stack_bounds
        right_axis_x = rect.x + pad_left + plot_w
        for i in range(y_ticks + 1):
            y_val = stack_min + (i / y_ticks) * (stack_max - stack_min)
            y = rect.y + pad_top + plot_h - (i / y_ticks) * plot_h
            pygame.draw.line(
                surface,
                AXIS_GRID,
                (right_axis_x, y),
                (right_axis_x + scale_ui(4), y),
                grid_width,
            )
            draw_text(
                surface,
                f"{y_val:.0f}",
                (right_axis_x + scale_ui(6), y - scale_ui(8)),
                size=tick_size,
                color=AXIS_LABEL,
            )

    # Axes labels
    draw_text(
        surface,
        f"last {settings.window_sec // 3600}h",
        (rect.right - pad_right - scale_ui(80), rect.y + scale_ui(6)),
        size=14,
        color=AXIS_LABEL,
    )

    def to_xy(pt: EvapPoint) -> Tuple[int, int]:
        x_frac = (pt.t_ms - start_t) / max(1, (latest_t - start_t))
        y_frac = (pt.flow - y_min) / max(1e-6, (y_max - y_min))
        x = rect.x + pad_left + x_frac * plot_w
        y = rect.y + pad_top + (1 - y_frac) * plot_h
        return int(x), int(y)

    if stack_bounds and stack_pts:
        stack_min, stack_max = stack_bounds

        def to_stack_xy(pt: StackPoint) -> Tuple[int, int]:
            x_frac = (pt.t_ms - start_t) / max(1, (latest_t - start_t))
            y_frac = (pt.temp_f - stack_min) / max(1e-6, (stack_max - stack_min))
            x = rect.x + pad_left + x_frac * plot_w
            y = rect.y + pad_top + (1 - y_frac) * plot_h
            return int(x), int(y)

        stack_width = max(1, scale_ui(2))
        dash_len = scale_ui(6)
        gap_len = scale_ui(4)
        stack_xy = [to_stack_xy(p) for p in stack_pts]
        draw_dashed_polyline(surface, STACK_LINE, stack_xy, dash_len, gap_len, stack_width)

    flow_width = max(1, scale_ui(2))
    for idx in range(1, len(pts)):
        p0 = pts[idx - 1]
        p1 = pts[idx]
        color = COLORS.get(p0.draw_off, COLORS["---"])
        pygame.draw.line(surface, color, to_xy(p0), to_xy(p1), flow_width)

    # Legend
    legend_y = x_label_y + tick_height + scale_ui(2)
    legend_x = rect.x + pad_left
    legend_items = [
        ("DO: ---", COLORS["---"]),
        ("DO: BROOK", COLORS["brookside"]),
        ("DO: ROAD", COLORS["roadside"]),
    ]
    legend_spacing = plot_w // len(legend_items)
    legend_line_len = scale_ui(16)
    legend_text_offset = scale_ui(6)
    legend_line_width = max(1, scale_ui(2))
    for i, (label, col) in enumerate(legend_items):
        lx = legend_x + i * legend_spacing
        ly = legend_y + scale_ui(8)
        pygame.draw.line(surface, col, (lx, ly), (lx + legend_line_len, ly), legend_line_width)
        draw_text(
            surface,
            label,
            (lx + legend_line_len + legend_text_offset, legend_y),
            size=14,
            color=TEXT_MUTED,
        )


def draw_status(surface, rect, status: EvapStatus):
    pygame.draw.rect(surface, CARD_BG, rect, border_radius=scale_ui(12))
    pad = scale_ui(6)
    row_size = 17
    row_gap = scale_ui(1)
    row_step = scale_font(row_size) + row_gap

    divider_x = rect.centerx
    divider_top = rect.y + pad
    divider_bottom = rect.bottom - pad
    pygame.draw.line(
        surface,
        TEXT_MAIN,
        (divider_x, divider_top),
        (divider_x, divider_bottom),
        max(1, scale_ui(1)),
    )

    left_col_x0 = rect.x + pad
    right_col_x0 = divider_x + scale_ui(6)

    def format_flow(val: Optional[float], with_space: bool = True) -> str:
        if val is None:
            return "–"
        try:
            num = int(round(float(val)))
        except (TypeError, ValueError):
            return "–"
        return f"{num} gph" if with_space else f"{num}gph"

    def short_tank(name: str) -> str:
        if not name or name == "---":
            return "---"
        key = name.strip().lower()
        return {"brookside": "Brook", "roadside": "Road"}.get(key, name.strip().title())

    flow_str = format_flow(status.evap_flow, with_space=True)
    do_flow = format_flow(status.draw_off_flow, with_space=True)
    pi_flow = format_flow(status.pump_in_flow, with_space=True)
    draw_off_name = short_tank(status.draw_off)
    pump_in_name = short_tank(status.pump_in)
    draw_off_str = f"{draw_off_name} ({do_flow})"
    pump_in_str = f"{pump_in_name} ({pi_flow})"

    last_fire_time = "--:-- --"
    last_fire_hours = "--.-hrs"
    if status.last_fire_min is not None:
        total_min = max(0, status.last_fire_min)
        base_dt = parse_iso(status.sample_ts) or datetime.now(timezone.utc)
        eta_dt = base_dt + timedelta(minutes=total_min)
        last_fire_time = eta_dt.strftime("%I:%M %p")
        last_fire_hours = f"{(total_min / 60.0):.1f}hrs"
    last_fire_value = f"{last_fire_time} ({last_fire_hours})"

    stack_temp_str = f"{status.stack_temp_f:.1f} F" if status.stack_temp_f is not None else "–"

    last_update_str = "--:--"
    sample_dt = parse_iso(status.sample_ts)
    if sample_dt:
        elapsed_sec = int((datetime.now(timezone.utc) - sample_dt).total_seconds())
        if elapsed_sec < 0:
            elapsed_sec = 0
        mins, secs = divmod(elapsed_sec, 60)
        last_update_str = f"{mins:02d}:{secs:02d}"

    system_time_str = datetime.now().strftime("%I:%M %p")
    if system_time_str.startswith("0"):
        system_time_str = system_time_str[1:]

    o2_str = "--"
    if status.o2_lambda is not None:
        o2_str = f"{status.o2_lambda:.3f} ({LAMBDA_SYMBOL})"

    left_rows = [
        f"Evap Flow: {flow_str}",
        f"Draw off: {draw_off_str}",
        f"Pump in: {pump_in_str}",
        f"Last fire: {last_fire_value}",
    ]
    right_rows = [
        f"Stack temp: {stack_temp_str}",
        f"O2: {o2_str}",
        f"Time: {system_time_str}",
        f"Last update: {last_update_str}",
    ]

    start_y = rect.y + pad
    for idx, text in enumerate(left_rows):
        y = start_y + idx * row_step
        draw_text(surface, text, (left_col_x0, y), size=row_size, color=TEXT_MAIN, bold=True)

    for idx, text in enumerate(right_rows):
        y = start_y + idx * row_step
        draw_text(surface, text, (right_col_x0, y), size=row_size, color=TEXT_MAIN, bold=True)


def disable_screen_blanking():
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"
    os.system("xset s off")      # disable screen saver
    os.system("xset -dpms")      # disable DPMS
    os.system("xset s noblank")  # disable blanking


def main():
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"
    debug_log(
        f"Init display with HAS_FONT={HAS_FONT} HAS_FREETYPE={HAS_FREETYPE} "
        f"API_BASE={API_BASE} STATUS_URL={STATUS_URL}"
    )
    pygame.init()
    disable_screen_blanking()
    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()

    settings = PlotSettings()
    points: List[EvapPoint] = []
    stack_points: List[StackPoint] = []
    status = EvapStatus(
        sample_ts=None,
        draw_off="---",
        draw_off_flow=None,
        pump_in="---",
        pump_in_flow=None,
        evap_flow=None,
        last_fire_min=None,
        stack_temp_f=None,
        o2_lambda=None,
    )
    last_fetch = 0.0
    last_snapshot = 0.0

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

        now = time.time()
        if now - last_fetch >= REFRESH_SEC:
            try:
                fetched = fetch_state()
                if fetched:
                    settings, points, stack_points, status = fetched
            except Exception as exc:
                debug_log(f"fetch_state error: {exc}")
            last_fetch = now

        screen.fill(BACKGROUND)
        outer_margin = scale_ui(6)
        section_gap = scale_ui(6)
        status_height = scale_ui(80)
        chart_rect = pygame.Rect(
            outer_margin,
            outer_margin,
            SCREEN_WIDTH - outer_margin * 2,
            SCREEN_HEIGHT - status_height - section_gap - outer_margin * 2,
        )
        status_rect = pygame.Rect(
            outer_margin,
            SCREEN_HEIGHT - status_height - outer_margin,
            SCREEN_WIDTH - outer_margin * 2,
            status_height,
        )
        draw_chart(screen, chart_rect, settings, points, stack_points)
        draw_status(screen, status_rect, status)

        pygame.display.flip()
        if SNAPSHOT_INTERVAL_SEC is not None and (now - last_snapshot) >= SNAPSHOT_INTERVAL_SEC:
            save_snapshot(screen, SNAPSHOT_PATH)
            last_snapshot = now
        clock.tick(30)

    pygame.quit()


if __name__ == "__main__":
    main()
