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

BACKGROUND = (16, 19, 26)
CARD_BG = (24, 29, 36)
TEXT_MAIN = (245, 245, 245)
TEXT_MUTED = (167, 175, 191)
AXIS_GRID = (60, 65, 75)
AXIS_LABEL = (167, 175, 191)
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

    status = EvapStatus(
        sample_ts=latest.get("sample_timestamp"),
        draw_off=(latest.get("draw_off_tank") or "---").lower(),
        draw_off_flow=latest.get("draw_off_flow_gph"),
        pump_in=(latest.get("pump_in_tank") or "---").lower(),
        pump_in_flow=latest.get("pump_in_flow_gph"),
        evap_flow=latest.get("evaporator_flow_gph"),
        last_fire_min=last_fire_min,
    )

    return settings, points, status


# ---- RENDERING ----


def draw_text(surface, text, pos, size=20, color=TEXT_MAIN, bold=False):
    key = (size, bool(bold))
    font_obj = _FONT_CACHE.get(key)

    if not font_obj and HAS_FONT:
        try:
            pg_font.init()
            font_obj = pg_font.SysFont(None, size, bold=bold) or pg_font.Font(None, size)
        except Exception:
            try:
                font_obj = pg_font.Font(None, size)
            except Exception:
                font_obj = None

    if not font_obj and HAS_FREETYPE:
        try:
            pg_ft.init()
            font_obj = pg_ft.SysFont(None, size, bold=bold)
        except Exception:
            font_obj = None

    if font_obj:
        _FONT_CACHE[key] = font_obj
    else:
        return

    try:
        if hasattr(font_obj, "render_to"):
            font_obj.render_to(surface, pos, text, color)
        else:
            render = font_obj.render(text, True, color)
            surface.blit(render, pos)
    except Exception:
        return


def draw_chart(surface, rect, settings: PlotSettings, points: List[EvapPoint]):
    pygame.draw.rect(surface, CARD_BG, rect, border_radius=12)
    if not points:
        draw_text(surface, "No evaporator data", (rect.x + 10, rect.y + 10), size=18, color=TEXT_MUTED)
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

    pad_left = 60
    pad_right = 10
    pad_top = 20
    pad_bottom = 40
    plot_w = rect.width - pad_left - pad_right
    plot_h = rect.height - pad_top - pad_bottom

    # Grid + ticks
    x_ticks = 5
    for i in range(x_ticks + 1):
        x = rect.x + pad_left + (i / x_ticks) * plot_w
        pygame.draw.line(surface, AXIS_GRID, (x, rect.y + pad_top), (x, rect.y + pad_top + plot_h), 1)
        t_tick = start_t + (i / x_ticks) * (latest_t - start_t)
        tick_dt = datetime.fromtimestamp(t_tick / 1000, tz=timezone.utc)
        tick_label = tick_dt.strftime("%H:%M")
        draw_text(surface, tick_label, (x - 18, rect.y + pad_top + plot_h + 14), size=14, color=AXIS_LABEL)

    y_ticks = 5
    for i in range(y_ticks + 1):
        y_val = y_min + (i / y_ticks) * (y_max - y_min)
        y = rect.y + pad_top + plot_h - (i / y_ticks) * plot_h
        pygame.draw.line(surface, AXIS_GRID, (rect.x + pad_left, y), (rect.x + pad_left + plot_w, y), 1)
        draw_text(surface, f"{y_val:.0f}", (rect.x + pad_left - 34, y - 10), size=14, color=AXIS_LABEL)

    # Axes labels
    label_y = rect.y + pad_top + plot_h / 2 - 10
    gph_surface = pygame.Surface((40, 20), pygame.SRCALPHA)
    draw_text(gph_surface, "gph", (0, 0), size=14, color=AXIS_LABEL, bold=True)
    gph_rot = pygame.transform.rotate(gph_surface, 90)
    surface.blit(gph_rot, (rect.x + 6, label_y))
    draw_text(
        surface,
        f"last {settings.window_sec // 3600}h",
        (rect.x + pad_left + plot_w // 2 - 30, rect.y + pad_top + plot_h + 30),
        size=14,
        color=AXIS_LABEL,
    )

    def to_xy(pt: EvapPoint) -> Tuple[int, int]:
        x_frac = (pt.t_ms - start_t) / max(1, (latest_t - start_t))
        y_frac = (pt.flow - y_min) / max(1e-6, (y_max - y_min))
        x = rect.x + pad_left + x_frac * plot_w
        y = rect.y + pad_top + (1 - y_frac) * plot_h
        return int(x), int(y)

    for idx in range(1, len(pts)):
        p0 = pts[idx - 1]
        p1 = pts[idx]
        color = COLORS.get(p0.draw_off, COLORS["---"])
        pygame.draw.line(surface, color, to_xy(p0), to_xy(p1), 3)

    # Legend
    legend_y = rect.y + pad_top + plot_h + 56
    legend_x = rect.x + pad_left
    legend_items = [
        ("Draw Off: ---", COLORS["---"]),
        ("Draw Off: BROOKSIDE", COLORS["brookside"]),
        ("Draw Off: ROADSIDE", COLORS["roadside"]),
    ]
    for i, (label, col) in enumerate(legend_items):
        lx = legend_x + i * 220
        pygame.draw.line(surface, col, (lx, legend_y), (lx + 30, legend_y), 4)
        draw_text(surface, label, (lx + 40, legend_y - 10), size=14, color=TEXT_MUTED)


def draw_status(surface, rect, status: EvapStatus):
    pygame.draw.rect(surface, CARD_BG, rect, border_radius=12)
    x, y = rect.x + 14, rect.y + 12
    row_y = y + 8
    ts_str = status.sample_ts or "–"
    try:
        dt = datetime.fromisoformat(ts_str)
        ts_str = dt.strftime("%H:%M")
    except Exception:
        pass

    flow_str = f"{status.evap_flow:.1f} gph" if status.evap_flow is not None else "–"
    last_fire_str = "---"
    if status.last_fire_min is not None:
        hrs = int(status.last_fire_min // 60)
        mins = int(status.last_fire_min % 60)
        last_fire_str = f"{hrs:02d}:{mins:02d}"
    draw_text(surface, f"Evap: {flow_str}", (x, row_y), size=22, color=TEXT_MAIN, bold=True)
    draw_text(surface, f"Time {ts_str}", (x, row_y + 24), size=16, color=TEXT_MUTED)
    draw_text(surface, f"Draw Off: {status.draw_off.upper()}", (x + 260, row_y), size=20, color=TEXT_MAIN, bold=True)
    do_flow = f"{status.draw_off_flow:.1f} gph" if status.draw_off_flow is not None else "–"
    draw_text(surface, f"{do_flow}", (x + 260, row_y + 24), size=16, color=TEXT_MUTED)
    draw_text(surface, f"Pump In: {status.pump_in.upper()}", (x + 500, row_y), size=20, color=TEXT_MAIN, bold=True)
    pi_flow = f"{status.pump_in_flow:.1f} gph" if status.pump_in_flow is not None else "–"
    draw_text(surface, f"{pi_flow}", (x + 500, row_y + 24), size=16, color=TEXT_MUTED)
    draw_text(surface, f"Last Fire In: {last_fire_str}", (x + 260, row_y + 48), size=18, color=TEXT_MAIN, bold=True)


def disable_screen_blanking():
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"
    os.system("xset s off")      # disable screen saver
    os.system("xset -dpms")      # disable DPMS
    os.system("xset s noblank")  # disable blanking


def main():
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"
    pygame.init()
    disable_screen_blanking()
    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()

    settings = PlotSettings()
    points: List[EvapPoint] = []
    status = EvapStatus(None, "---", None, "---", None, None)
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
            fetched = fetch_state(settings.window_sec if settings else None)
            if fetched:
                settings, points, status = fetched
            last_fetch = now

        screen.fill(BACKGROUND)
        chart_rect = pygame.Rect(16, 16, SCREEN_WIDTH - 32, SCREEN_HEIGHT - 180)
        status_rect = pygame.Rect(16, SCREEN_HEIGHT - 140, SCREEN_WIDTH - 32, 110)
        draw_chart(screen, chart_rect, settings, points)
        draw_status(screen, status_rect, status)

        pygame.display.flip()
        clock.tick(30)

    pygame.quit()


if __name__ == "__main__":
    main()
