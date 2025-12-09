import datetime as dt
import json
import signal
import sqlite3
import time
from dataclasses import dataclass
from multiprocessing import Queue
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

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
    "instant_gph",
    "filtered_gph",
    "is_valid",
    "fault_code",
    "pump_event_flag",
]


@dataclass
class TankComputationConfig:
    num_to_average: int
    meas_delay: float
    readings_per_min: float
    rate_update_seconds: int
    fill_threshold_gph: float
    empty_threshold_gph: float
    guard_max_in_gph: float
    guard_max_out_gph: float
    empty_gal_floor: float
    near_empty_gal: float
    near_empty_window_min: float
    fill_window_min: float
    fill_window_max: float
    draw_window_min: float
    draw_window_max: float
    transfer_tank_gal: float
    pump_spike_min_gph: float
    pump_spike_max_gph: float
    pump_end_tolerance_gph: float
    pump_end_consecutive: int

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
    measurement_config: TankComputationConfig,
    *,
    clock=None,
    debug_records: Optional[Sequence[DebugSample]] = None,
    history_db_path: Optional[Path] = None,
    status_dir: Optional[Path] = None,
    history_hours: int = DEFAULT_HISTORY_HOURS,
    loop_debug: bool = False,
    loop_gap_seconds: float = 10.0,
):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Ensure SIGTERM follows default behavior so parent terminate()/kill() works.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    reading_wait_time = dt.timedelta(seconds=60 / measurement_config.readings_per_min)
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
        measurement_config,
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
    update_time = _clock_now(clock) - dt.timedelta(days=1)
    while True:
        now = _clock_now(clock)
        if now > update_time:
            update_time = now + reading_wait_time
            measurement = tank.update_status()
            if measurement and status_queue:
                status_queue.put(measurement)
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
        time.sleep(0.1)


class TANK:
    def __init__(
        self,
        tank_name,
        uart,
        config: TankComputationConfig,
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
        self.history_db_path = Path(history_db_path) if history_db_path else None
        self.history_hours = history_hours
        self.status_writer = TankStatusFileWriter(status_dir, tank_name)
        self.error_queue = error_queue
        self.config = config
        self.num_to_average = config.num_to_average
        self.delay = config.meas_delay
        self.readings_per_min = config.readings_per_min
        self.rate_update_dt = dt.timedelta(seconds=config.rate_update_seconds)
        self.fill_threshold = config.fill_threshold_gph
        self.empty_threshold = config.empty_threshold_gph
        self.guard_max_in_gph = config.guard_max_in_gph
        self.guard_max_out_gph = config.guard_max_out_gph
        self.empty_gal_floor = config.empty_gal_floor
        self.near_empty_gal = config.near_empty_gal
        self.near_empty_window_min = config.near_empty_window_min
        self.fill_window_min = config.fill_window_min
        self.fill_window_max = config.fill_window_max
        self.draw_window_min = config.draw_window_min
        self.draw_window_max = config.draw_window_max
        self.transfer_tank_gal = config.transfer_tank_gal
        self.pump_spike_min_gph = config.pump_spike_min_gph
        self.pump_spike_max_gph = config.pump_spike_max_gph
        self.pump_end_tolerance_gph = config.pump_end_tolerance_gph
        self.pump_end_consecutive = config.pump_end_consecutive
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
        self.mins_back = config.fill_window_min
        self.filling = False
        self.emptying = False
        self.remaining_time = None
        self.filtered_flow = None
        self.current_window_minutes = config.fill_window_min
        self.last_filtered_timestamp = None
        self.pending_pump_correction = 0.0
        self.pump_event_active = False
        self.pump_spike_streak = 0
        self.pump_end_streak = 0
        self.pump_start_volume = None
        self.pre_event_flow = None
        self.last_status_payload = None
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
        self._last_error_message = None
        self._last_error_at = None

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
                SELECT source_timestamp, surf_dist, depth, volume_gal,
                       instant_gph, filtered_gph, is_valid, fault_code, pump_event_flag
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
                "instant_gph": row["instant_gph"] if "instant_gph" in row.keys() else None,
                "filtered_gph": row["filtered_gph"] if "filtered_gph" in row.keys() else None,
                "is_valid": row["is_valid"] if "is_valid" in row.keys() else 1,
                "fault_code": row["fault_code"] if "fault_code" in row.keys() else None,
                "pump_event_flag": row["pump_event_flag"] if "pump_event_flag" in row.keys() else 0,
            }
            records.append(record)
        if not records:
            return df
        history_df = pd.DataFrame(records)
        for col, default in (
            ("instant_gph", None),
            ("filtered_gph", None),
            ("is_valid", 1),
            ("fault_code", None),
            ("pump_event_flag", 0),
        ):
            if col not in history_df:
                history_df[col] = default
        return history_df[df_col_order]

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
        self.dist_to_surf = np.mean(cur_readings) if cur_readings else None

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
        ts = ensure_utc(measurement_time)
        flow_fields = self._process_flow(ts)
        row = self._build_history_row(ts, flow_fields)
        self._append_history(row)
        self._prune_history_if_needed(ts)
        self._update_remaining_time(ts, flow_fields)

        db_payload = self._build_payload(ts, flow_fields, include_extended=True)
        if flow_fields["is_valid"] and not flow_fields["fault_code"]:
            status_payload = self._build_payload(ts, flow_fields, include_extended=False)
            self.status_writer.write(status_payload)
            self.last_status_payload = status_payload
        elif self.last_status_payload:
            # Leave the on-disk status at the last good measurement.
            pass
        return db_payload

    def _build_history_row(self, ts: dt.datetime, flow_fields: Dict[str, object]) -> Dict[str, object]:
        return {
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
            "instant_gph": flow_fields.get("instant_gph"),
            "filtered_gph": flow_fields.get("filtered_gph"),
            "is_valid": 1 if flow_fields.get("is_valid") else 0,
            "fault_code": flow_fields.get("fault_code"),
            "pump_event_flag": 1 if flow_fields.get("pump_event_flag") else 0,
        }

    def _append_history(self, row: Dict[str, object]):
        self.history_df = pd.concat([self.history_df, pd.DataFrame([row])], ignore_index=True)

    def _prune_history_if_needed(self, now: dt.datetime):
        if now < self.next_prune_time:
            return
        cutoff = now - dt.timedelta(hours=self.history_hours)
        self.history_df = self.history_df[self.history_df["datetime"] >= cutoff]
        self.history_df.reset_index(drop=True, inplace=True)
        self.next_prune_time = now + HISTORY_PRUNE_INTERVAL

    def update_mins_back(self, mins_back):
        self.mins_back += mins_back
        self.mins_back = max([self.draw_window_min, self.mins_back])
        self.mins_back = min([self.fill_window_max, self.mins_back])
        self.current_window_minutes = self.mins_back

    def _latest_good_row(self, include_pump: bool = False) -> Optional[pd.Series]:
        if self.history_df.empty:
            return None
        mask = self.history_df["is_valid"] == 1
        if not include_pump:
            mask &= self.history_df["pump_event_flag"] == 0
        good = self.history_df.loc[mask]
        if good.empty:
            return None
        return good.iloc[-1]

    def _median_good_gallons(self, n: int = 10) -> Optional[float]:
        if self.history_df.empty:
            return None
        mask = self.history_df["is_valid"] == 1
        good = self.history_df.loc[mask]
        if good.empty:
            return None
        return float(good.tail(n)["gal"].median())

    def _compute_instant_flow(
        self, ts: dt.datetime, apply_pending_correction: bool = True
    ) -> Dict[str, object]:
        last_good = self._latest_good_row()
        if last_good is None:
            return {"instant_gph": None, "dt_seconds": None, "reference_row": None}

        dt_seconds = (ts - ensure_utc(last_good["datetime"])).total_seconds()
        if dt_seconds <= 0:
            return {"instant_gph": None, "dt_seconds": dt_seconds, "reference_row": last_good}

        correction = self.pending_pump_correction if apply_pending_correction else 0.0
        effective_current = (self.current_gallons or 0) - correction
        delta_gal = effective_current - (last_good["gal"] or 0)
        instant_gph = (delta_gal / dt_seconds) * 3600

        if apply_pending_correction and correction:
            # Apply the correction only once, on the first post-event valid reading.
            self.pending_pump_correction = 0.0

        if min(self.current_gallons or 0, last_good["gal"] or 0) < self.empty_gal_floor and instant_gph < 0:
            instant_gph = 0.0

        return {
            "instant_gph": instant_gph,
            "dt_seconds": dt_seconds,
            "reference_row": last_good,
            "effective_current": effective_current,
        }

    def _apply_guardrails(
        self, instant_gph: Optional[float], dt_seconds: Optional[float], effective_current: Optional[float]
    ) -> Dict[str, object]:
        if instant_gph is None or dt_seconds is None or dt_seconds <= 0:
            return {"is_valid": True, "fault_code": None}

        fault_code = None
        if instant_gph > self.guard_max_in_gph or instant_gph < -self.guard_max_out_gph:
            fault_code = "flow_bounds"
        else:
            median_gal = self._median_good_gallons(10)
            if median_gal is not None:
                rate_vs_median = ((effective_current or 0) - median_gal) / (dt_seconds / 3600)
                if rate_vs_median > self.guard_max_in_gph or rate_vs_median < -self.guard_max_out_gph:
                    fault_code = "median_flow_bounds"

        return {"is_valid": fault_code is None, "fault_code": fault_code}

    def _detect_pump_event(self, instant_gph: Optional[float]) -> Dict[str, object]:
        pump_event_flag = False
        pump_ended = False
        pump_volume_added = 0.0

        if instant_gph is None:
            self.pump_spike_streak = 0
            if not self.pump_event_active:
                self.pump_end_streak = 0
            return {
                "pump_event_flag": pump_event_flag,
                "pump_ended": pump_ended,
                "pump_volume_added": pump_volume_added,
            }

        if self.pump_event_active:
            pump_event_flag = True
            if abs(instant_gph - (self.pre_event_flow or 0)) <= self.pump_end_tolerance_gph:
                self.pump_end_streak += 1
            else:
                self.pump_end_streak = 0

            if self.pump_end_streak >= self.pump_end_consecutive:
                pump_ended = True
                pump_event_flag = True
                self.pump_event_active = False
                self.pump_end_streak = 0
                self.pump_spike_streak = 0
                if self.pump_start_volume is not None and self.current_gallons is not None:
                    pump_volume_added = max((self.current_gallons or 0) - (self.pump_start_volume or 0), 0)
                self.pump_start_volume = None
        else:
            if self.pump_spike_min_gph <= instant_gph <= self.pump_spike_max_gph:
                self.pump_spike_streak += 1
            else:
                self.pump_spike_streak = 0

            if self.pump_spike_streak >= 2:
                self.pump_event_active = True
                self.pump_start_volume = self.current_gallons
                self.pre_event_flow = self.filtered_flow if self.filtered_flow is not None else instant_gph
                pump_event_flag = True
                self.pump_end_streak = 0
        return {
            "pump_event_flag": pump_event_flag,
            "pump_ended": pump_ended,
            "pump_volume_added": pump_volume_added,
        }

    def _update_filter_window(self, instant_gph: Optional[float], ts: dt.datetime) -> None:
        if instant_gph is None:
            return
        dt_seconds = (
            (ts - self.last_filtered_timestamp).total_seconds()
            if self.last_filtered_timestamp
            else 0
        )
        if self.pump_event_active:
            return

        target_window = self.current_window_minutes
        if instant_gph < self.empty_threshold:
            # Shrink quickly toward the draw-off target.
            target_window = max(self.draw_window_min, min(self.current_window_minutes, self.draw_window_max))
            if self.current_window_minutes > self.draw_window_min:
                decay = min(1.0, dt_seconds / 90.0) if dt_seconds else 1.0
                self.current_window_minutes = max(
                    self.draw_window_min,
                    self.current_window_minutes - (self.current_window_minutes - self.draw_window_min) * decay,
                )
        elif instant_gph > self.fill_threshold:
            # Grow slowly toward the estimated pump interval.
            estimated_minutes = (self.transfer_tank_gal / max(instant_gph, 0.001)) * 60
            target_window = max(self.fill_window_min, min(estimated_minutes, self.fill_window_max))
            step = 0.1 * (target_window - self.current_window_minutes)
            self.current_window_minutes = max(
                self.fill_window_min, min(self.fill_window_max, self.current_window_minutes + step)
            )
        else:
            # Idle/steady: drift toward a moderate window.
            target_window = max(self.fill_window_min, min(self.current_window_minutes, self.fill_window_max))

        if (self.current_gallons or 0) < self.near_empty_gal and instant_gph >= self.empty_threshold:
            self.current_window_minutes = max(self.current_window_minutes, self.near_empty_window_min)
        self.current_window_minutes = max(self.draw_window_min, min(self.current_window_minutes, self.fill_window_max))
        self.mins_back = self.current_window_minutes

    def _update_filtered_flow(
        self,
        instant_gph: Optional[float],
        ts: dt.datetime,
        *,
        is_valid: bool,
        pump_event_flag: bool,
    ) -> Optional[float]:
        if not is_valid or pump_event_flag or instant_gph is None:
            return self.filtered_flow

        if self.last_filtered_timestamp is None:
            self.filtered_flow = instant_gph
            self.last_filtered_timestamp = ts
            return self.filtered_flow

        dt_seconds = (ts - self.last_filtered_timestamp).total_seconds()
        if dt_seconds <= 0:
            return self.filtered_flow

        window_seconds = max(self.current_window_minutes * 60, 1)
        alpha = min(1.0, dt_seconds / window_seconds)
        self.filtered_flow = self.filtered_flow + alpha * (instant_gph - self.filtered_flow)
        self.last_filtered_timestamp = ts
        return self.filtered_flow

    def _process_flow(self, ts: dt.datetime) -> Dict[str, object]:
        apply_pending = not self.pump_event_active
        base_flow = self._compute_instant_flow(ts, apply_pending_correction=apply_pending)
        instant_gph = base_flow.get("instant_gph")
        dt_seconds = base_flow.get("dt_seconds")
        effective_current = base_flow.get("effective_current")

        guardrail = self._apply_guardrails(instant_gph, dt_seconds, effective_current)
        is_valid = guardrail["is_valid"]
        fault_code = guardrail["fault_code"]

        pump_state = self._detect_pump_event(instant_gph if is_valid else None)
        pump_event_flag = pump_state["pump_event_flag"]
        if pump_state["pump_ended"] and pump_state["pump_volume_added"] > 0:
            self.pending_pump_correction += pump_state["pump_volume_added"]
        if pump_event_flag:
            self.filtered_flow = self.filtered_flow  # keep previous

        self._update_filter_window(instant_gph, ts)
        filtered = self._update_filtered_flow(
            instant_gph, ts, is_valid=is_valid, pump_event_flag=pump_event_flag
        )

        self.tank_rate = None
        self.filling = False
        self.emptying = False
        if filtered is not None and is_valid and not pump_event_flag:
            self.tank_rate = float(np.round(filtered, 1))
            if self.tank_rate > self.fill_threshold:
                self.filling = True
            elif self.tank_rate < self.empty_threshold:
                self.emptying = True

        if fault_code:
            error_msg = (
                f"Reading rejected ({fault_code}); dist={self.dist_to_surf}, "
                f"depth={self.depth}, gallons={self.current_gallons}, "
                f"instant_gph={instant_gph}"
            )
            self._emit_error(error_msg, ts)

        return {
            "instant_gph": instant_gph,
            "filtered_gph": self.tank_rate if self.tank_rate is not None else filtered,
            "is_valid": is_valid,
            "fault_code": fault_code,
            "pump_event_flag": pump_event_flag,
        }

    def _update_remaining_time(self, now: dt.datetime, flow_fields: Dict[str, object]) -> None:
        if now > self.next_rate_update:
            self.next_rate_update = now + self.rate_update_dt

        if not flow_fields.get("is_valid") or flow_fields.get("pump_event_flag"):
            return
        self.remaining_time = None
        self.eta_full = None
        self.eta_empty = None
        self.time_to_full_min = None
        self.time_to_empty_min = None
        if self.filling and self.tank_rate:
            gallons_remaining = max(self.max_vol - (self.current_gallons or 0), 0)
            if self.tank_rate > 0:
                hours = gallons_remaining / self.tank_rate
                self.remaining_time = dt.timedelta(hours=hours)
                eta = now + self.remaining_time
                self.eta_full = eta.isoformat()
                self.time_to_full_min = hours * 60
        if self.emptying and self.tank_rate:
            gallons_remaining = max(self.current_gallons or 0, 0)
            if self.tank_rate < 0:
                hours = gallons_remaining / abs(self.tank_rate)
                self.remaining_time = dt.timedelta(hours=hours)
                eta = now + self.remaining_time
                self.eta_empty = eta.isoformat()
                self.time_to_empty_min = hours * 60

    def get_tank_rate(self, current_time=None):
        now = ensure_utc(current_time or self._now())
        flow_fields = {
            "is_valid": True,
            "fault_code": None,
            "pump_event_flag": False,
            "instant_gph": None,
            "filtered_gph": self.tank_rate if self.tank_rate is not None else self.filtered_flow,
        }
        self._update_remaining_time(now, flow_fields)
        return self.tank_rate

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

        if self.filling and self.remaining_time:
            remaining_hrs = np.round(self.remaining_time.total_seconds() / 3600, 1)
            remaining_time_prefix = "Full"
            state["rate_str"] = "{}gals/hr over previous {}mins".format(
                self.tank_rate, self.mins_back
            )
            state["remaining_time"] = "{} at {} ({}hrs)".format(
                remaining_time_prefix,
                (self._now() + self.remaining_time).strftime("%I:%M%p"),
                remaining_hrs,
            )
        if self.emptying and self.remaining_time:
            remaining_hrs = np.round(self.remaining_time.total_seconds() / 3600, 1)
            remaining_time_prefix = "Empty"
            state["rate_str"] = "{}gals/hr over previous {}mins".format(
                self.tank_rate, self.mins_back
            )
            state["remaining_time"] = "{} at {} ({}hrs)".format(
                remaining_time_prefix,
                (self._now() + self.remaining_time).strftime("%I:%M%p"),
                remaining_hrs,
            )

        state["mins_back"] = self.mins_back
        return state

    def _build_payload(
        self, measurement_time: dt.datetime, flow_fields: Dict[str, object], *, include_extended: bool
    ) -> Dict[str, Optional[float]]:
        percent_full = None
        if self.current_gallons is not None and self.max_vol:
            percent_full = max(
                0.0,
                min(100.0, (float(self.current_gallons) / float(self.max_vol)) * 100.0),
            )
        flow_value = (
            self.tank_rate if flow_fields.get("is_valid") and not flow_fields.get("pump_event_flag") else None
        )
        payload = {
            "generated_at": ensure_utc(self._now()).isoformat(),
            "tank_id": self.name,
            "source_timestamp": ensure_utc(measurement_time).isoformat(),
            "surf_dist": self.dist_to_surf,
            "depth": self.depth,
            "volume_gal": self.current_gallons,
            "flow_gph": flow_value,
            "eta_full": self.eta_full,
            "eta_empty": self.eta_empty,
            "time_to_full_min": self.time_to_full_min,
            "time_to_empty_min": self.time_to_empty_min,
            "level_percent": percent_full,
            "max_volume_gal": self.max_vol,
        }
        if include_extended:
            payload.update(
                {
                    "instant_gph": flow_fields.get("instant_gph"),
                    "filtered_gph": flow_fields.get("filtered_gph"),
                    "is_valid": 1 if flow_fields.get("is_valid") else 0,
                    "fault_code": flow_fields.get("fault_code"),
                    "pump_event_flag": 1 if flow_fields.get("pump_event_flag") else 0,
                }
            )
        return payload

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
