import datetime as dt
import json
from collections import deque
import signal
import sqlite3
import time
from dataclasses import dataclass
from multiprocessing import Queue
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from hampel import hampel

try:
    import adafruit_character_lcd.character_lcd_rgb_i2c as character_lcd
    import board
    import busio
except ImportError:  # pragma: no cover - only used on Pi hardware
    character_lcd = None
    board = None
    busio = None

try:
    import RPi.GPIO as GPIO
except ImportError:  # pragma: no cover - only used on Pi hardware
    GPIO = None

try:
    import serial
except ImportError:  # pragma: no cover - serial hardware not available on dev hosts
    serial = None

if GPIO is not None:  # pragma: no cover - only runs on Pi
    GPIO.setmode(GPIO.BCM)

df_col_order = [
    "datetime",
    "timestamp",
    "yr",
    "mo",
    "day",
    "hr",
    "m",
    "s",
    "surf_dist",
    "depth",
    "gal",
    "depth_outlier",
    "flow_gph",
    "flow_window_min",
]

tank_names = ["brookside", "roadside"]
SERIAL_PORTS = {
    "brookside": "/dev/serial0",
    "roadside": "/dev/ttyAMA5",
}
DEFAULT_HISTORY_HOURS = 6
HISTORY_PRUNE_INTERVAL = dt.timedelta(hours=1)

queue_dict = {}
for tank_name in tank_names:
    queue_dict[tank_name] = {
        "command": Queue(),
        "response": Queue(),
        "screen_response": Queue(),
        "status_updates": Queue(),
    "errors": Queue(),
}


@dataclass
class FlowSettings:
    flow_window_minutes: float = 6.0
    hampel_window_size: int = 50
    hampel_n_sigma: float = 0.25
    adapt_short_flow_window_min: float = 3.0
    adapt_neg_window_min: float = 15.0
    adapt_pos_max_window_min: float = 100.0
    adapt_window_update_delay: int = 2
    adapt_step: float = 0.1
    low_flow_threshold: float = 20.0
    large_neg_flow_gph: float = 100.0

    @classmethod
    def from_env(cls, env: Dict[str, str]):
        """Build flow settings from environment values when available."""
        def _get(name, default):
            val = env.get(name)
            if val is None:
                return default
            try:
                return type(default)(val)
            except Exception:
                return default

        return cls(
            flow_window_minutes=_get("TANK_FLOW_WINDOW_MINUTES", cls.flow_window_minutes),
            hampel_window_size=_get("TANK_HAMPEL_WINDOW", cls.hampel_window_size),
            hampel_n_sigma=_get("TANK_HAMPEL_SIGMA", cls.hampel_n_sigma),
            adapt_short_flow_window_min=_get(
                "TANK_ADAPT_SHORT_FLOW_WINDOW_MIN", cls.adapt_short_flow_window_min
            ),
            adapt_neg_window_min=_get("TANK_ADAPT_NEG_WINDOW_MIN", cls.adapt_neg_window_min),
            adapt_pos_max_window_min=_get("TANK_ADAPT_POS_MAX_WINDOW_MIN", cls.adapt_pos_max_window_min),
            adapt_window_update_delay=_get(
                "TANK_ADAPT_WINDOW_UPDATE_DELAY", cls.adapt_window_update_delay
            ),
            adapt_step=_get("TANK_ADAPT_STEP", cls.adapt_step),
            low_flow_threshold=_get("TANK_LOW_FLOW_THRESHOLD", cls.low_flow_threshold),
            large_neg_flow_gph=_get("TANK_LARGE_NEG_FLOW_GPH", cls.large_neg_flow_gph),
        )


def calc_gallons_interp(df, length):
    df.loc[0, "gals_interp"] = 0
    for i in df.index[1:]:
        bottom_width = df.loc[i - 1, "widths"]
        top_width = df.loc[i, "widths"]
        bottom = df.loc[i - 1, "depths"]
        top = df.loc[i, "depths"]
        vol = length * (top - bottom) * (top_width + bottom_width) / 2
        gals = np.round(vol / 231, 3)
        df.loc[i, "gals_interp"] = df.loc[i - 1, "gals_interp"] + gals

    return df


def calculate_checksum(buffer):
    return (buffer[0] + buffer[1] + buffer[2]) & 0xFF


brookside_length = 191.5
brookside_width = 48
brookside_height = 40.75
brookside_radius = 17
brookside_depths = [
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13.25,
    20,
    25,
    30,
    36.5,
]
brookside_widths = [
    10,
    19.25,
    27.25,
    33.25,
    37,
    39.5,
    41.5,
    43,
    44,
    45.5,
    46.5,
    47.375,
    47.75,
    48,
    48,
    48,
    48,
    48,
]
brookside_dimension_df = pd.DataFrame({"depths": brookside_depths, "widths": brookside_widths})
brookside_dimension_df = calc_gallons_interp(brookside_dimension_df, brookside_length)

roadside_length = 178.5
roadside_width = 54
roadside_height = 39
roadside_radius = 18
roadside_depths = [0, 2.75, 3.75, 4.75, 5.75, 6.75, 8.75, 9.75, 10.75, 11.75, 16, 20, 25, 30, 35, 39]
roadside_widths = [
    22,
    38.75,
    41.125,
    43.625,
    45.375,
    46.25,
    48.5,
    49.25,
    50.75,
    51.75,
    54,
    54,
    54,
    54,
    54,
    54,
]
roadside_dimension_df = pd.DataFrame({"depths": roadside_depths, "widths": roadside_widths})
roadside_dimension_df = calc_gallons_interp(roadside_dimension_df, roadside_length)

tank_dims_dict = {
    "brookside": {
        "length": brookside_length,
        "width": brookside_width,
        "height": brookside_height,
        "radius": brookside_radius,
        "dim_df": brookside_dimension_df,
        "bottom_dist": 55.125,
    },
    "roadside": {
        "length": roadside_length,
        "width": roadside_width,
        "height": roadside_height,
        "radius": roadside_radius,
        "dim_df": roadside_dimension_df,
        "bottom_dist": 50.75,
    },
}


def ensure_utc(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc)


def _safe_float(value: Optional[float]) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_hampel_depth(depth_series: pd.Series, window_size: int, n_sigma: float):
    depth_numeric = pd.to_numeric(depth_series, errors="coerce")
    if depth_numeric.empty:
        return depth_numeric, pd.Series(dtype=bool)
    window_size = int(window_size) if window_size is not None else 0
    if window_size % 2 == 0:
        window_size += 1
    window = max(3, min(window_size, len(depth_numeric)))
    res = hampel(depth_numeric.to_numpy(), window_size=window, n_sigma=float(n_sigma))
    filtered = pd.Series(res.filtered_data, index=depth_numeric.index)
    outliers = pd.Series(False, index=depth_numeric.index)
    if getattr(res, "outlier_indices", None) is not None:
        outliers.iloc[res.outlier_indices] = True
    return filtered, outliers


def compute_adaptive_trailing_flow(
    timestamps: pd.Series,
    gallons: pd.Series,
    is_outlier: pd.Series,
    settings: FlowSettings,
):
    """Compute per-point flow using trailing adaptive window with negative-flow guard."""
    flows = pd.Series(np.nan, index=gallons.index, dtype=float)
    windows_used = pd.Series(np.nan, index=gallons.index, dtype=float)

    short_window = pd.Timedelta(minutes=settings.adapt_short_flow_window_min)
    flow_history = deque(maxlen=max(1, int(settings.adapt_window_update_delay)))
    current_window_min = settings.flow_window_minutes
    prev_window_min = current_window_min
    last_large_neg_ts = None
    last_large_neg_window = None

    timestamps = pd.to_datetime(timestamps)
    valid_mask = (~is_outlier) & (~gallons.isna())

    for idx in gallons.index:
        if not valid_mask.loc[idx]:
            continue
        ts = timestamps.loc[idx]

        short_mask = (
            valid_mask
            & (timestamps >= ts - short_window)
            & (timestamps <= ts)
        )
        short_df = gallons.loc[short_mask]
        short_flow = np.nan
        if len(short_df) >= 2:
            times_ns = timestamps.loc[short_mask].astype("int64")
            t0 = times_ns.iloc[0]
            hours = (times_ns - t0) / 3.6e12
            if not np.all(hours == 0):
                slope, _ = np.polyfit(hours, short_df, 1)
                short_flow = float(slope)
                flow_history.append(short_flow)

        target_window = current_window_min
        if flow_history:
            abs_all_lt = all(abs(v) < settings.low_flow_threshold for v in flow_history)
            all_neg = all(v < 0 for v in flow_history)
            all_pos = all(v > 0 for v in flow_history)
            if abs_all_lt:
                target_window = settings.adapt_pos_max_window_min
            elif all_neg:
                target_window = settings.adapt_neg_window_min
            elif all_pos:
                mean_flow = float(np.mean(flow_history))
                target_window = max(1200.0 / max(mean_flow, 1e-6), settings.adapt_pos_max_window_min)

        current_window_min = current_window_min + (target_window - current_window_min) * settings.adapt_step
        current_window_min = max(current_window_min, 0.1)

        proposed_window_min = current_window_min
        trailing_window_min = proposed_window_min
        if (
            last_large_neg_ts is not None
            and proposed_window_min > prev_window_min
            and (ts - pd.Timedelta(minutes=proposed_window_min)) < last_large_neg_ts
        ):
            delta_minutes = max(0.0, (ts - last_large_neg_ts).total_seconds() / 60.0)
            guard_window = last_large_neg_window if last_large_neg_window is not None else 0.0
            max_allowed = max(guard_window, delta_minutes)
            trailing_window_min = min(proposed_window_min, max_allowed)

        trailing_window = pd.Timedelta(minutes=trailing_window_min)
        window_mask = (
            valid_mask
            & (timestamps >= ts - trailing_window)
            & (timestamps <= ts)
        )
        window_gals = gallons.loc[window_mask]
        if len(window_gals) < 2:
            prev_window_min = trailing_window_min
            continue

        times_ns = timestamps.loc[window_mask].astype("int64")
        t0 = times_ns.iloc[0]
        hours = (times_ns - t0) / 3.6e12
        if np.all(hours == 0):
            prev_window_min = trailing_window_min
            continue

        slope, _ = np.polyfit(hours, window_gals, 1)
        flows.at[idx] = float(slope)
        windows_used.at[idx] = trailing_window_min

        if slope < -settings.large_neg_flow_gph:
            last_large_neg_ts = ts
            last_large_neg_window = trailing_window_min

        prev_window_min = trailing_window_min

    return flows, windows_used




def depth_to_gallons(tank_name: str, depth: float) -> float:
    """Convert a depth reading to gallons using tank geometry."""
    dims = tank_dims_dict[tank_name]
    dim_df = dims["dim_df"]
    length = dims["length"]
    depth = max(0.0, min(depth, dim_df["depths"].max()))

    ind = dim_df.loc[dim_df["depths"] <= depth].index[-1]
    bottom_depth = dim_df.loc[ind, "depths"]
    gallons = dim_df.loc[ind, "gals_interp"]

    if depth > bottom_depth:
        bottom_width = dim_df.loc[ind, "widths"]
        top_width = np.interp(depth, dim_df["depths"], dim_df["widths"])
        vol = length * (depth - bottom_depth) * (bottom_width + top_width) / 2
        gallons += vol / 231.0

    return float(np.round(gallons, 2))


@dataclass
class DebugSample:
    timestamp: dt.datetime
    surf_dist: float
    depth: Optional[float] = None
    volume_gal: Optional[float] = None
    flow_gph: Optional[float] = None


class DebugTankFeed:
    """Replay CSV readings in sync with a SyntheticClock."""

    def __init__(
        self,
        records: Sequence[DebugSample],
        clock=None,
        loop: bool = False,
        loop_gap_seconds: float = 10.0,
    ):
        self.records = sorted(records, key=lambda rec: rec.timestamp)
        self.clock = clock
        self.loop = loop
        self.loop_gap = dt.timedelta(seconds=loop_gap_seconds)
        self.index = 0
        if len(self.records) >= 2:
            self.cycle_span = self.records[-1].timestamp - self.records[0].timestamp
        else:
            self.cycle_span = dt.timedelta(seconds=0)
        self.offset = dt.timedelta(0)

    def next_sample(self) -> Optional[DebugSample]:
        if not self.records:
            return None
        if self.index >= len(self.records):
            if not self.loop:
                return None
            self.index = 0
            span = self.cycle_span
            if span <= dt.timedelta(0):
                span = self.loop_gap
            self.offset += span + self.loop_gap
        base_sample = self.records[self.index]
        scheduled = base_sample.timestamp + self.offset
        if self.clock:
            self.clock.wait_until(scheduled)
        self.index += 1
        return DebugSample(
            timestamp=scheduled,
            surf_dist=base_sample.surf_dist,
            depth=base_sample.depth,
            volume_gal=base_sample.volume_gal,
            flow_gph=base_sample.flow_gph,
        )


class TankStatusFileWriter:
    """Atomically write per-tank status JSON files without locking hassles."""

    def __init__(self, status_dir: Optional[Path], tank_id: str):
        self.path = None
        if status_dir:
            status_dir = Path(status_dir)
            status_dir.mkdir(parents=True, exist_ok=True)
            self.path = status_dir / f"status_{tank_id}.json"

    def write(self, payload: Dict) -> None:
        if not self.path:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)


def init_display():
    if None in (character_lcd, board, busio):  # pragma: no cover - requires hardware
        raise RuntimeError("LCD hardware libraries not available on this host")
    lcd_columns = 16
    lcd_rows = 2
    i2c = busio.I2C(board.SCL, board.SDA)
    lcd = character_lcd.Character_LCD_RGB_I2C(i2c, lcd_columns, lcd_rows)
    lcd.color = [100, 0, 0]
    return lcd


def run_lcd_screen(lcd, queue_dict):  # pragma: no cover - hardware specific
    # Restore default SIGTERM handling so parent terminate() works.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    lcd.clear()
    lcd.message = "HELLO"
    time.sleep(2)
    lcd.clear()

    next_update = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    screen_update_frequency = dt.timedelta(seconds=15)

    tank_ctr = 0
    param_ctr = 0
    prev_msg = "none"
    cur_msg = "weekend"

    while True:
        now = dt.datetime.now(dt.timezone.utc)
        if now > next_update:
            next_update = now + screen_update_frequency
            responses = {}
            for ind, tank_name in enumerate(tank_names):
                queue_dict[tank_name]["command"].put("update_screen")
                while queue_dict[tank_name]["screen_response"].empty():
                    time.sleep(0.1)
                responses[ind] = queue_dict[tank_name]["screen_response"].get()

        if tank_ctr in responses.keys():
            if param_ctr in responses[tank_ctr].keys():
                cur_msg = "{}\n{}".format(responses[tank_ctr]["name"], responses[tank_ctr][param_ctr])
            else:
                print("{} param ctr not in keys: {}".format(param_ctr, responses[tank_ctr].keys()))
        else:
            print("{} not in keys: {}".format(tank_ctr, responses.keys()))
        if cur_msg != prev_msg:
            lcd.clear()
            lcd.message = cur_msg
            prev_msg = cur_msg

        if lcd.down_button:
            tank_ctr -= 1
        if lcd.up_button:
            tank_ctr += 1
        if lcd.left_button:
            param_ctr -= 1
        if lcd.right_button:
            param_ctr += 1
        tank_ctr %= 2
        param_ctr %= 3


def _open_uart(port: Optional[str]):
    if not port or serial is None:
        return None
    try:
        return serial.Serial(port, baudrate=9600, timeout=0.5)
    except Exception as exc:  # pragma: no cover - only on Pi
        print(f"Unable to open UART {port}: {exc}")
        return None


def _clock_now(clock):
    now = clock.now() if clock else dt.datetime.now(dt.timezone.utc)
    return ensure_utc(now)


def run_tank_controller(
    tank_name,
    queue_dict,
    measurement_rate_params,
    *,
    flow_settings: Optional[FlowSettings] = None,
    clock=None,
    debug_records: Optional[Sequence[DebugSample]] = None,
    history_db_path: Optional[Path] = None,
    status_dir: Optional[Path] = None,
    history_hours: int = DEFAULT_HISTORY_HOURS,
    loop_debug: bool = False,
    loop_gap_seconds: float = 10.0,
):
    flow_settings = flow_settings or FlowSettings()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Ensure SIGTERM follows default behavior so parent terminate()/kill() works.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    (
        num_to_average,
        delay,
        readings_per_min,
        window_size,
        n_sigma,
        rate_update_dt,
    ) = measurement_rate_params
    reading_wait_time = dt.timedelta(seconds=60 / readings_per_min)
    command_queue = queue_dict[tank_name]["command"]
    response_queue = queue_dict[tank_name]["response"]
    screen_response_queue = queue_dict[tank_name]["screen_response"]
    status_queue = queue_dict[tank_name]["status_updates"]
    error_queue = queue_dict[tank_name]["errors"]
    debug_feed = (
        DebugTankFeed(
            debug_records,
            clock,
            loop=loop_debug,
            loop_gap_seconds=loop_gap_seconds,
        )
        if debug_records
        else None
    )
    uart = None if debug_feed else _open_uart(SERIAL_PORTS.get(tank_name))

    tank = TANK(
        tank_name,
        uart,
        num_to_average,
        delay,
        window_size,
        n_sigma,
        rate_update_dt,
        flow_settings=flow_settings,
        tank_dims_dict=tank_dims_dict,
        clock=clock,
        debug_feed=debug_feed,
        history_db_path=history_db_path,
        status_dir=status_dir,
        history_hours=history_hours,
        error_queue=error_queue,
    )
    if not debug_feed and uart is None and error_queue:
        error_queue.put(
            {
                "tank_id": tank_name,
                "message": f"UART unavailable on port {SERIAL_PORTS.get(tank_name)}",
                "source_timestamp": _clock_now(clock).isoformat(),
            }
        )
    update_time = _clock_now(clock)
    loop_sleep = 0.01 if clock else 0.1
    while True:
        now = _clock_now(clock)
        did_update = False
        while now >= update_time:
            measurement_payloads = tank.update_status()
            if measurement_payloads and status_queue:
                if isinstance(measurement_payloads, list):
                    for payload in measurement_payloads:
                        if payload:
                            status_queue.put(payload)
                else:
                    status_queue.put(measurement_payloads)
            update_time += reading_wait_time
            now = _clock_now(clock)
            did_update = True
        if not command_queue.empty():
            command = command_queue.get()
            parts = command.split(":")
            if len(parts) == 2:
                command = parts[0]
                command_val = int(parts[1])
            allowable_commands = [
                "update",
                "update_screen",
                "set_mins_back",
            ]
            if command in allowable_commands:
                if command == "update_screen":
                    tank.get_tank_rate()
                    screen_response_queue.put(tank.return_screen_data())
                else:
                    if command == "update":
                        tank.get_tank_rate()
                    if command == "set_mins_back":
                        tank.update_mins_back(command_val)
                        tank.get_tank_rate()
                    response_queue.put(tank.return_current_state())
        if not did_update:
            time.sleep(loop_sleep)


class TANK:
    def __init__(
        self,
        tank_name,
        uart,
        num_to_average,
        delay,
        window_size,
        n_sigma,
        rate_update_dt,
        flow_settings: Optional[FlowSettings] = None,
        tank_dims_dict=tank_dims_dict,
        clock=None,
        debug_feed: Optional[DebugTankFeed] = None,
        history_db_path: Optional[Path] = None,
        status_dir: Optional[Path] = None,
        history_hours: int = DEFAULT_HISTORY_HOURS,
        error_queue=None,
    ):
        self.name = tank_name
        self.uart = uart
        self.clock = clock
        self.debug_feed = debug_feed
        self.flow_settings = flow_settings or FlowSettings()
        self.history_db_path = Path(history_db_path) if history_db_path else None
        self.history_hours = history_hours
        self.status_writer = TankStatusFileWriter(status_dir, tank_name)
        self.error_queue = error_queue
        self.num_to_average = num_to_average
        self.delay = delay
        self.window_size = window_size
        self.n_sigma = n_sigma
        self.rate_update_dt = dt.timedelta(seconds=rate_update_dt)
        self.next_rate_update = self._now() - dt.timedelta(days=1)
        self.next_prune_time = self._now()
        self.uart_trigger = 0x55
        dims = tank_dims_dict[tank_name]
        self.length = dims["length"]
        self.width = dims["width"]
        self.height = dims["height"]
        self.radius = dims["radius"]
        self.dim_df = dims["dim_df"]
        self.bottom_dist = dims["bottom_dist"]
        self.mins_back = 30
        self.filling = False
        self.emptying = False
        self.remaining_time = None

        self.max_vol = int(round(max(self.dim_df.gals_interp), 0))
        self.history_df = self._load_recent_history()
        self.dist_to_surf = None
        self.depth = None
        self.current_gallons = None
        self.tank_rate = None
        self.eta_full = None
        self.eta_empty = None
        self.time_to_full_min = None
        self.time_to_empty_min = None
        self.last_flow_window_min = None
        self.last_depth_outlier = None
        self._last_error_message = None
        self._last_error_at = None
        self.hampel_window_size = self.flow_settings.hampel_window_size
        if self.hampel_window_size % 2 == 0:
            self.hampel_window_size += 1
        self.hampel_window_size = max(3, self.hampel_window_size)
        self._last_finalized_center_idx = -1
        self._ensure_history_columns()

    def _ensure_history_columns(self) -> None:
        """Ensure history_df has flow/outlier columns even when loaded from disk."""
        for col in ("depth_outlier", "flow_gph", "flow_window_min"):
            if col not in self.history_df.columns:
                self.history_df[col] = pd.Series([pd.NA] * len(self.history_df))
        self.history_df.reset_index(drop=True, inplace=True)

    def _latest_flow_row(self):
        if self.history_df.empty or "flow_gph" not in self.history_df.columns:
            return None
        mask = self.history_df["flow_gph"].notna()
        if not mask.any():
            return None
        true_indices = mask[mask].index
        if len(true_indices) == 0:
            return None
        return self.history_df.loc[true_indices[-1]]

    def _build_payload_from_history_idx(self, idx: int) -> Dict[str, Optional[float]]:
        row = self.history_df.loc[idx]
        depth_outlier_val = row.get("depth_outlier", pd.NA)
        outlier_flag = None
        if pd.notna(depth_outlier_val):
            outlier_flag = bool(depth_outlier_val)
        flow_val = row.get("flow_gph", pd.NA)
        flow_val = None if pd.isna(flow_val) else float(np.round(flow_val, 1))

        percent_full = None
        volume_val = row.get("gal")
        if pd.notna(volume_val) and self.max_vol:
            percent_full = max(0.0, min(100.0, (float(volume_val) / float(self.max_vol)) * 100.0))

        eta_full = None
        eta_empty = None
        time_to_full_min = None
        time_to_empty_min = None
        if flow_val:
            if flow_val > 0 and volume_val is not None:
                gallons_remaining = max(self.max_vol - float(volume_val), 0)
                hours = gallons_remaining / flow_val if flow_val != 0 else None
                if hours is not None:
                    eta_full = (ensure_utc(row["datetime"]) + dt.timedelta(hours=hours)).isoformat()
                    time_to_full_min = hours * 60
            elif flow_val < 0 and volume_val is not None:
                gallons_remaining = max(float(volume_val), 0)
                hours = gallons_remaining / abs(flow_val) if flow_val != 0 else None
                if hours is not None:
                    eta_empty = (ensure_utc(row["datetime"]) + dt.timedelta(hours=hours)).isoformat()
                    time_to_empty_min = hours * 60

        return {
            "generated_at": ensure_utc(self._now()).isoformat(),
            "tank_id": self.name,
            "source_timestamp": ensure_utc(row["datetime"]).isoformat(),
            "surf_dist": row.get("surf_dist"),
            "depth": row.get("depth"),
            "depth_outlier": outlier_flag,
            "volume_gal": volume_val if pd.notna(volume_val) else None,
            "flow_gph": flow_val,
            "eta_full": eta_full,
            "eta_empty": eta_empty,
            "time_to_full_min": time_to_full_min,
            "time_to_empty_min": time_to_empty_min,
            "level_percent": percent_full,
            "max_volume_gal": self.max_vol,
        }

    def _finalize_center_sample(self) -> Optional[Dict[str, object]]:
        """
        When enough points exist, compute Hampel outlier + flow for the center
        of the latest window and return a payload for that center row.
        """
        if len(self.history_df) < self.hampel_window_size:
            return None

        center_offset = self.hampel_window_size // 2
        center_idx = len(self.history_df) - center_offset - 1
        if center_idx <= self._last_finalized_center_idx:
            return None

        window_df = self.history_df.iloc[-self.hampel_window_size :]
        _, outliers = apply_hampel_depth(
            window_df["depth"],
            window_size=self.hampel_window_size,
            n_sigma=self.flow_settings.hampel_n_sigma,
        )
        center_outlier = bool(outliers.iloc[center_offset]) if len(outliers) > center_offset else False
        global_idx = len(self.history_df) - self.hampel_window_size + center_offset
        self.history_df.loc[global_idx, "depth_outlier"] = center_outlier

        up_to_center = self.history_df.iloc[: global_idx + 1].copy()
        gallons_series = up_to_center["depth"].apply(
            lambda d: depth_to_gallons(self.name, d) if d is not None and not pd.isna(d) else np.nan
        )
        outlier_series = (
            up_to_center["depth_outlier"]
            .astype("boolean")
            .fillna(False)
            .astype(bool, copy=False)
        )
        flows, windows_used = compute_adaptive_trailing_flow(
            up_to_center["datetime"],
            gallons_series,
            outlier_series,
            self.flow_settings,
        )
        flow_val = flows.iloc[-1]
        window_val = windows_used.iloc[-1]
        self.history_df.loc[global_idx, "flow_gph"] = flow_val
        self.history_df.loc[global_idx, "flow_window_min"] = window_val

        self._last_finalized_center_idx = global_idx
        return self._build_payload_from_history_idx(global_idx)

    def _now(self):
        return _clock_now(self.clock)

    def _emit_error(self, message: str, ts: Optional[dt.datetime] = None) -> None:
        if not self.error_queue:
            return
        now = ts or self._now()
        now_ts = ensure_utc(now).isoformat()
        if message == self._last_error_message and self._last_error_at:
            delta = (ensure_utc(now).timestamp() - ensure_utc(self._last_error_at).timestamp())
            if delta < 60:
                return
        self._last_error_message = message
        self._last_error_at = ensure_utc(now)
        try:
            self.error_queue.put(
                {
                    "tank_id": self.name,
                    "message": message,
                    "source_timestamp": now_ts,
                }
            )
        except Exception:
            pass

    def _load_recent_history(self):
        df = pd.DataFrame(columns=df_col_order)
        if not self.history_db_path or not self.history_db_path.exists():
            return df
        since = self._now() - dt.timedelta(hours=self.history_hours)
        try:
            conn = sqlite3.connect(self.history_db_path)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return df
        try:
            cur = conn.execute(
                """
                SELECT source_timestamp, surf_dist, depth, volume_gal
                FROM tank_readings
                WHERE tank_id = ? AND source_timestamp >= ?
                ORDER BY source_timestamp
                """,
                (self.name, ensure_utc(since).isoformat()),
            )
            rows = cur.fetchall()
        except sqlite3.Error:
            conn.close()
            return df
        conn.close()

        records = []
        for row in rows:
            ts_raw = row["source_timestamp"]
            try:
                ts = ensure_utc(dt.datetime.fromisoformat(ts_raw))
            except ValueError:
                continue
            record = {
                "datetime": ts,
                "timestamp": ts.isoformat(),
                "yr": ts.year,
                "mo": ts.month,
                "day": ts.day,
                "hr": ts.hour,
                "m": ts.minute,
                "s": ts.second,
                "surf_dist": row["surf_dist"],
                "depth": row["depth"],
                "gal": row["volume_gal"],
            }
            records.append(record)
        if not records:
            return df
        history_df = pd.DataFrame(records)
        return history_df.reindex(columns=df_col_order)

    def read_distance(self):
        if not self.uart:
            return None
        self.uart.write(bytes([self.uart_trigger]))
        time.sleep(0.1)
        distance = None
        if self.uart.in_waiting > 0:
            time.sleep(0.004)
            if self.uart.read(1) == b"\xff":
                buffer_RTT = self.uart.read(3)
                if len(buffer_RTT) == 3:
                    cs = calculate_checksum(b"\xff" + buffer_RTT)
                    if buffer_RTT[2] == cs:
                        distance = (buffer_RTT[0] << 8) + buffer_RTT[1]
        return distance

    def get_average_distance(self):
        if not self.uart:
            self.dist_to_surf = None
            return
        cur_readings = []
        for _ in range(self.num_to_average):
            distance = self.read_distance()
            if distance is not None:
                distance /= 25.4
                distance = np.round(distance, 2)
                cur_readings.append(distance)
            time.sleep(self.delay)
        self.dist_to_surf = np.median(cur_readings) if cur_readings else None

    def _next_debug_distance(self):
        if not self.debug_feed:
            return None, None
        sample = self.debug_feed.next_sample()
        if not sample:
            return None, None
        return ensure_utc(sample.timestamp), _safe_float(sample.surf_dist)

    def update_status(self):
        if self.debug_feed:
            sample_time, dist = self._next_debug_distance()
            if sample_time is None:
                return None
            self.dist_to_surf = dist
            measurement_time = sample_time
        else:
            self.get_average_distance()
            measurement_time = self._now()

        if self.dist_to_surf is None:
            self._emit_error("No distance measurement", measurement_time)
            return None

        self.get_gal_in_tank()
        self._append_history(measurement_time)
        self._prune_history_if_needed(measurement_time)
        center_payload = self.get_tank_rate(measurement_time)

        latest_idx = self.history_df.index[-1]
        latest_payload = self._build_payload_from_history_idx(latest_idx)

        payloads = [latest_payload]
        if center_payload:
            payloads.append(center_payload)

        self.status_writer.write(latest_payload)
        return payloads

    def _append_history(self, measurement_time):
        ts = ensure_utc(measurement_time)
        row = {
            "datetime": ts,
            "timestamp": ts.isoformat(),
            "yr": ts.year,
            "mo": ts.month,
            "day": ts.day,
            "hr": ts.hour,
            "m": ts.minute,
            "s": ts.second,
            "surf_dist": self.dist_to_surf,
            "depth": self.depth,
            "gal": self.current_gallons,
            "depth_outlier": pd.NA,
            "flow_gph": pd.NA,
            "flow_window_min": pd.NA,
        }
        if self.history_df.empty:
            self.history_df = pd.DataFrame([row]).reindex(columns=df_col_order)
        else:
            self.history_df.loc[len(self.history_df)] = row

    def _prune_history_if_needed(self, now):
        if now < self.next_prune_time:
            return
        before_len = len(self.history_df)
        cutoff = now - dt.timedelta(hours=self.history_hours)
        self.history_df = self.history_df[self.history_df["datetime"] >= cutoff]
        self.history_df.reset_index(drop=True, inplace=True)
        after_len = len(self.history_df)
        if before_len != after_len and self._last_finalized_center_idx >= 0:
            dropped = before_len - after_len
            self._last_finalized_center_idx = max(-1, self._last_finalized_center_idx - dropped)
        self.next_prune_time = now + HISTORY_PRUNE_INTERVAL

    def update_mins_back(self, mins_back):
        self.mins_back += mins_back
        self.mins_back = max([5, self.mins_back])
        self.mins_back = min([240, self.mins_back])

    def get_tank_rate(self, current_time=None):
        now = ensure_utc(current_time or self._now())
        self._ensure_history_columns()
        center_payload = self._finalize_center_sample()

        # Track most recent finalized flow for display/debug.
        flow_row = self._latest_flow_row()
        if flow_row is not None:
            flow_val = flow_row.get("flow_gph")
            window_val = flow_row.get("flow_window_min")
            self.tank_rate = None if pd.isna(flow_val) else float(np.round(flow_val, 1))
            self.last_flow_window_min = None if pd.isna(window_val) else float(window_val)
            self.last_depth_outlier = bool(flow_row.get("depth_outlier")) if pd.notna(flow_row.get("depth_outlier")) else None
            self.filling = self.tank_rate is not None and self.tank_rate > 5
            self.emptying = self.tank_rate is not None and self.tank_rate < -5
            if self.tank_rate and self.current_gallons is not None:
                if self.tank_rate > 0:
                    gallons_remaining = max(self.max_vol - self.current_gallons, 0)
                    hours = gallons_remaining / self.tank_rate if self.tank_rate != 0 else None
                    if hours is not None:
                        self.remaining_time = dt.timedelta(hours=hours)
                        self.eta_full = (now + self.remaining_time).isoformat()
                        self.time_to_full_min = hours * 60
                        self.eta_empty = None
                        self.time_to_empty_min = None
                elif self.tank_rate < 0:
                    gallons_remaining = max(self.current_gallons, 0)
                    hours = gallons_remaining / abs(self.tank_rate) if self.tank_rate != 0 else None
                    if hours is not None:
                        self.remaining_time = dt.timedelta(hours=hours)
                        self.eta_empty = (now + self.remaining_time).isoformat()
                        self.time_to_empty_min = hours * 60
                        self.eta_full = None
                        self.time_to_full_min = None
        return center_payload

    def return_screen_data(self):
        state = {}
        state["name"] = self.name
        rate_display = f"{self.tank_rate:.1f}" if isinstance(self.tank_rate, (int, float)) else "ND"
        gallons_display = int(self.current_gallons or 0)
        state[0] = "{} | {}gph".format(gallons_display, rate_display)
        state[1] = 'raw dst: {}"'.format(self.dist_to_surf)
        state[2] = 'depth: {}"'.format(self.depth)
        return state

    def return_current_state(self):
        state = {}
        state["name"] = self.name
        state["current_gallons"] = str(np.round(self.current_gallons or 0, 0))
        state["rate"] = str(self.tank_rate if self.tank_rate is not None else "ND")
        state["filling"] = str(self.filling)
        state["emptying"] = str(self.emptying)
        state["rate_str"] = "---"
        state["remaining_time"] = "N/A"
        try:
            state["dist_to_surf"] = str(np.round(self.dist_to_surf, 3))
            state["depth"] = str(np.round(self.depth, 3))
        except Exception:
            state["dist_to_surf"] = "???"
            state["depth"] = "???"

        window_str = (
            f"{self.last_flow_window_min:.1f}"
            if isinstance(self.last_flow_window_min, (int, float))
            else str(self.flow_settings.flow_window_minutes)
        )
        if self.filling and self.remaining_time:
            remaining_hrs = np.round(self.remaining_time.total_seconds() / 3600, 1)
            remaining_time_prefix = "Full"
            state["rate_str"] = "{}gals/hr over trailing {}mins".format(
                self.tank_rate, window_str
            )
            state["remaining_time"] = "{} at {} ({}hrs)".format(
                remaining_time_prefix,
                (self._now() + self.remaining_time).strftime("%I:%M%p"),
                remaining_hrs,
            )
        if self.emptying and self.remaining_time:
            remaining_hrs = np.round(self.remaining_time.total_seconds() / 3600, 1)
            remaining_time_prefix = "Empty"
            state["rate_str"] = "{}gals/hr over trailing {}mins".format(
                self.tank_rate, window_str
            )
            state["remaining_time"] = "{} at {} ({}hrs)".format(
                remaining_time_prefix,
                (self._now() + self.remaining_time).strftime("%I:%M%p"),
                remaining_hrs,
            )

        state["mins_back"] = self.mins_back
        state["flow_window_min"] = window_str
        return state

    def get_gal_in_tank(self):
        depth = self.bottom_dist - (self.dist_to_surf or 0)
        self.raw_depth = np.round(depth, 2)
        depth = max([0, depth])
        depth = min([depth, self.dim_df["depths"].max()])
        self.depth = depth

        print(
            "{}:\nbottom_dist: {}\ndist_reading: {}\nraw_depth: {}\nadjusted_depth:{}\n".format(
                self.name, self.bottom_dist, self.dist_to_surf, self.raw_depth, depth
            )
        )

        ind = self.dim_df.loc[self.dim_df["depths"] <= depth].index[-1]
        bottom_depth = self.dim_df.loc[ind, "depths"]
        gallons = self.dim_df.loc[ind, "gals_interp"]

        if depth > bottom_depth:
            bottom_width = self.dim_df.loc[ind, "widths"]
            top_width = np.interp(depth, self.dim_df["depths"], self.dim_df["widths"])
            vol = self.length * (depth - bottom_depth) * (bottom_width + top_width) / 2
            gallons += vol / 231

        self.current_gallons = np.round(gallons, 2)
