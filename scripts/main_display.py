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
from datetime import datetime, timezone
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
HISTORY_URL = os.environ.get(
    "DISPLAY_HISTORY_URL", f"{API_BASE}/evaporator_history.php"
)
REFRESH_SEC = float(os.environ.get("DISPLAY_REFRESH_SEC", "15"))
WINDOW_DEFAULT = 2 * 60 * 60  # 2h
YMIN_DEFAULT = 200.0
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
    "brookside": (0, 114, 178),  # blue (colorblind-friendly)
    "roadside": (213, 94, 0),    # orange (colorblind-friendly)
}

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
class EvapStatus:
    sample_ts: Optional[str]
    draw_off: str
    draw_off_flow: Optional[float]
    pump_in: str
    pump_in_flow: Optional[float]
    evap_flow: Optional[float]
    last_fire_min: Optional[float] = None
    stack_temp_f: Optional[float] = None


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


def fetch_state(preferred_window_sec: Optional[int]) -> Tuple[PlotSettings, List[EvapPoint], EvapStatus]:
    cache_bust = int(time.time())
    status_payload = fetch_json(STATUS_URL, params={"t": cache_bust}) or {}
    stack_payload = fetch_json(STACK_STATUS_URL, params={"t": cache_bust}) or {}
    brook_status = fetch_json(f"{BASE_URL}/data/status_brookside.json", params={"t": cache_bust}) or {}
    road_status = fetch_json(f"{BASE_URL}/data/status_roadside.json", params={"t": cache_bust}) or {}
    history_params = {"t": cache_bust}
    if preferred_window_sec:
        history_params["window_sec"] = preferred_window_sec
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

    status = EvapStatus(
        sample_ts=latest.get("sample_timestamp"),
        draw_off=(latest.get("draw_off_tank") or "---").lower(),
        draw_off_flow=latest.get("draw_off_flow_gph"),
        pump_in=(latest.get("pump_in_tank") or "---").lower(),
        pump_in_flow=latest.get("pump_in_flow_gph"),
        evap_flow=latest.get("evaporator_flow_gph"),
        last_fire_min=last_fire_min,
        stack_temp_f=stack_temp,
    )

    return settings, points, status


# ---- RENDERING ----


def scale_font(size: int) -> int:
    return int(round(size * FONT_SCALE))


def scale_ui(value: float) -> int:
    return int(round(value * UI_SCALE))


def draw_text(surface, text, pos, size=20, color=TEXT_MAIN, bold=False):
    global _WARN_NO_FONT, _WARN_RENDER_FAIL
    scaled_size = scale_font(size)
    key = (scaled_size, bool(bold))
    font_obj = _FONT_CACHE.get(key)

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
    else:
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


def draw_chart(surface, rect, settings: PlotSettings, points: List[EvapPoint]):
    pygame.draw.rect(surface, CARD_BG, rect, border_radius=scale_ui(12))
    if not points:
        draw_text(
            surface,
            "No evaporator data",
            (rect.x + scale_ui(10), rect.y + scale_ui(10)),
            size=18,
            color=TEXT_MUTED,
        )
        return

    y_min = settings.y_min
    y_max = settings.y_max if settings.y_max > y_min else y_min + 1
    window_ms = settings.window_sec * 1000
    latest_t = points[-1].t_ms
    start_t = latest_t - window_ms
    pts = [p for p in points if p.t_ms >= start_t]
    if not pts:
        pts = points[-100:]  # fallback
        start_t = pts[0].t_ms
        latest_t = pts[-1].t_ms

    pad_left = scale_ui(45)
    pad_right = scale_ui(8)
    pad_top = scale_ui(12)
    pad_bottom = scale_ui(36)
    plot_w = rect.width - pad_left - pad_right
    plot_h = rect.height - pad_top - pad_bottom

    tick_size = 14
    tick_gap = scale_ui(2)
    tick_height = scale_font(tick_size) + tick_gap
    grid_width = max(1, scale_ui(1))

    # Grid + ticks
    x_ticks = 5
    x_label_y = rect.y + pad_top + plot_h + scale_ui(2)
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

    # Axes labels
    label_y = rect.y + pad_top + plot_h / 2 - scale_ui(10)
    gph_surface = pygame.Surface((scale_ui(40), scale_ui(20)), pygame.SRCALPHA)
    draw_text(gph_surface, "gph", (0, 0), size=14, color=AXIS_LABEL, bold=True)
    gph_rot = pygame.transform.rotate(gph_surface, 90)
    surface.blit(gph_rot, (rect.x + scale_ui(4), label_y))
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
    pad = scale_ui(8)
    col_gap = scale_ui(6)
    row_gap = scale_ui(4)
    label_size = 16
    value_size = 20
    value_emphasis_size = 22
    note_size = 14
    last_fire_size = 18
    line_gap = scale_ui(2)

    def line_height(size: int) -> int:
        return scale_font(size) + line_gap

    label_h = line_height(label_size)
    row1_value_h = line_height(value_emphasis_size)
    note_h = line_height(note_size)
    row1_h = label_h + row1_value_h + note_h

    inner_x = rect.x + pad
    inner_y = rect.y + pad
    inner_w = rect.width - pad * 2

    col_w = (inner_w - col_gap * 2) // 3
    row2_col_w = (inner_w - col_gap) // 2
    row1_y = inner_y
    row2_y = row1_y + row1_h + row_gap

    def draw_block(x, y, label, value, note=None, value_size_override=None):
        value_size_local = value_size_override or value_size
        draw_text(surface, label, (x, y), size=label_size, color=TEXT_MUTED)
        value_y = y + label_h
        draw_text(surface, value, (x, value_y), size=value_size_local, color=TEXT_MAIN, bold=True)
        if note is not None:
            note_y = value_y + line_height(value_size_local)
            draw_text(surface, note, (x, note_y), size=note_size, color=TEXT_MUTED)

    ts_str = status.sample_ts or "–"
    try:
        dt = datetime.fromisoformat(ts_str)
        ts_str = dt.strftime("%H:%M")
    except Exception:
        pass

    flow_str = f"{status.evap_flow:.1f} gph" if status.evap_flow is not None else "–"
    do_flow = f"{status.draw_off_flow:.1f} gph" if status.draw_off_flow is not None else "–"
    pi_flow = f"{status.pump_in_flow:.1f} gph" if status.pump_in_flow is not None else "–"
    last_fire_str = "---"
    if status.last_fire_min is not None:
        hrs = int(status.last_fire_min // 60)
        mins = int(status.last_fire_min % 60)
        last_fire_str = f"{hrs:02d}:{mins:02d}"
    stack_temp_str = f"{status.stack_temp_f:.1f} F" if status.stack_temp_f is not None else "–"

    draw_block(
        inner_x,
        row1_y,
        "Evap Flow",
        flow_str,
        f"Time {ts_str}",
        value_size_override=value_emphasis_size,
    )
    draw_block(
        inner_x + col_w + col_gap,
        row1_y,
        "Draw Off",
        status.draw_off.upper(),
        do_flow,
    )
    draw_block(
        inner_x + (col_w + col_gap) * 2,
        row1_y,
        "Pump In",
        status.pump_in.upper(),
        pi_flow,
    )
    draw_block(
        inner_x,
        row2_y,
        "Last Fire In",
        last_fire_str,
        value_size_override=last_fire_size,
    )
    draw_block(
        inner_x + row2_col_w + col_gap,
        row2_y,
        "Stack Temp",
        stack_temp_str,
    )


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
    status = EvapStatus(
        sample_ts=None,
        draw_off="---",
        draw_off_flow=None,
        pump_in="---",
        pump_in_flow=None,
        evap_flow=None,
        last_fire_min=None,
        stack_temp_f=None,
    )
    last_fetch = 0.0

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
                fetched = fetch_state(settings.window_sec if settings else None)
                if fetched:
                    settings, points, status = fetched
            except Exception as exc:
                debug_log(f"fetch_state error: {exc}")
            last_fetch = now

        screen.fill(BACKGROUND)
        outer_margin = scale_ui(6)
        section_gap = scale_ui(6)
        status_height = scale_ui(120)
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
        draw_chart(screen, chart_rect, settings, points)
        draw_status(screen, status_rect, status)

        pygame.display.flip()
        clock.tick(30)

    pygame.quit()


if __name__ == "__main__":
    main()
