#!/usr/bin/env python3
"""Hampel-filter depth readings, convert to gallons, and plot flow slopes.

Steps:
- Load all roadside/brookside CSVs from smoothing_testing/dataset.
- Sort by timestamp and flag Hampel outliers on depth.
- Convert non-outlier depths to gallons using tank_vol_fcns logic.
- Compute per-point flow (gallons/hour) from a linear fit over a time window.
- Plot depth (with outliers), gallons, and flow; save plots to smoothing_testing/plots.
- Write per-tank CSVs with timestamp, depth, outlier flag, gallons, and flow.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
from typing import Dict, Optional, Tuple
from collections import deque

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from hampel import hampel
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tank_vol_fcns as TVF  # noqa: E402

# ------------------
# Config parameters.
# ------------------
FLOW_WINDOW_MINUTES = 6  # initial trailing window used for per-point slope (gal/hour)
HAMP_WINDOW_SIZE = 50  # points in Hampel window for depth filtering
HAMP_N_SIGMA = 0.25
# Adaptive moving window config
ADAPT_SHORT_FLOW_WINDOW_MIN = 3  # window to compute short-term flow used to steer the MA window
ADAPT_NEG_WINDOW_MIN = 15  # target window (minutes) when recent flows are all negative
ADAPT_POS_MAX_WINDOW_MIN = 100  # cap for target window when filling
ADAPT_WINDOW_UPDATE_DELAY = 2  # number of recent flow estimates to consider for targeting
ADAPT_STEP = 1 / 10  # fraction to move the window toward target each update
LOW_FLOW_THRESHOLD = 20  # gph threshold to consider flows ~zero
LARGE_NEG_FLOW_GPH = 100  # gph magnitude to freeze back-expansion until window passes this point

DATASET_DIR = Path(__file__).resolve().parent / "dataset"
PLOT_DIR = Path(__file__).resolve().parent / "plots"
OUTPUT_CSV_DIR = Path(__file__).resolve().parent


def _make_tank_converter(tank_name: str) -> TVF.TANK:
    """Build a lightweight TANK instance for gallon conversion only."""
    return TVF.TANK(
        tank_name,
        uart=None,
        num_to_average=1,
        delay=0,
        window_size=1,
        n_sigma=1,
        rate_update_dt=60,
        tank_dims_dict=TVF.tank_dims_dict,
        history_db_path=None,
        status_dir=None,
    )


_TANK_CONVERTERS: Dict[str, TVF.TANK] = {name: _make_tank_converter(name) for name in TVF.tank_names}


def gallons_from_depth(tank_name: str, depth_in: Optional[float]) -> float:
    """Use tank_vol_fcns.TANK.get_gal_in_tank to convert depth (inches) to gallons."""
    if depth_in is None or pd.isna(depth_in):
        return np.nan
    tank = _TANK_CONVERTERS[tank_name]
    tank.dist_to_surf = tank.bottom_dist - float(depth_in)
    with contextlib.redirect_stdout(io.StringIO()):
        tank.get_gal_in_tank()
    return float(tank.current_gallons or np.nan)


def load_tank_data(tank_name: str) -> pd.DataFrame:
    csv_paths = sorted(DATASET_DIR.glob(f"{tank_name}_*.csv"))
    frames = []
    dims = TVF.tank_dims_dict[tank_name]
    bottom_dist = dims["bottom_dist"]
    max_depth = dims["dim_df"]["depths"].max()
    for path in tqdm(csv_paths, desc=f"Loading {tank_name} CSVs"):
        df = pd.read_csv(path)
        df = df.loc[:, ~df.columns.str.contains(r"^Unnamed")]
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df["surf_dist"] = pd.to_numeric(df.get("surf_dist"), errors="coerce")
        depth_measured = pd.to_numeric(df.get("depth"), errors="coerce")

        depths = []
        gallons = []
        for idx, row in tqdm(df.iterrows(), total=len(df), desc=f"Depth+gal {tank_name}", leave=False):
            surf = row["surf_dist"]
            row_depth_measured = depth_measured.loc[idx] if idx in depth_measured.index else np.nan
            depth_val = np.nan
            gallons_val = np.nan
            if pd.notna(surf):
                tank = _TANK_CONVERTERS[tank_name]
                tank.dist_to_surf = float(surf)
                with contextlib.redirect_stdout(io.StringIO()):
                    tank.get_gal_in_tank()
                depth_val = tank.depth
                gallons_val = tank.current_gallons
            elif pd.notna(row_depth_measured):
                depth_val = float(row_depth_measured)
                gallons_val = gallons_from_depth(tank_name, depth_val)

            depths.append(depth_val)
            gallons.append(gallons_val)

        df["depth"] = pd.Series(depths).clip(lower=0, upper=max_depth)
        df["gallons_raw"] = gallons
        df["tank"] = tank_name
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["timestamp", "depth", "tank"])
    data = pd.concat(frames, ignore_index=True)
    data = data.sort_values("timestamp").reset_index(drop=True)
    return data


def apply_hampel_depth(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["is_outlier"] = False
    df["depth_hampel"] = df["depth"]

    if df.empty:
        return df

    depth_series = df["depth"].to_numpy()
    window = max(3, min(HAMP_WINDOW_SIZE, len(df)))
    res = hampel(depth_series, window_size=window, n_sigma=HAMP_N_SIGMA)
    df["depth_hampel"] = res.filtered_data
    outlier_indices = getattr(res, "outlier_indices", None)
    if outlier_indices is not None:
        df.loc[df.index[outlier_indices], "is_outlier"] = True
    return df


def compute_flow(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["gallons"] = np.nan
    df["flow_gph"] = np.nan
    df["flow_window_min"] = np.nan

    tank_label = str(df["tank"].iloc[0]) if not df.empty else "tank"

    for idx, row in tqdm(df.iterrows(), total=len(df), desc=f"Gallons {tank_label}"):
        if row["is_outlier"]:
            continue
        base_gal = row.get("gallons_raw") if "gallons_raw" in df else np.nan
        if pd.isna(base_gal):
            base_gal = gallons_from_depth(row["tank"], row["depth"])
        df.at[idx, "gallons"] = base_gal

    if df["timestamp"].isnull().all():
        return df

    short_window = pd.Timedelta(minutes=ADAPT_SHORT_FLOW_WINDOW_MIN)
    flow_history = deque(maxlen=ADAPT_WINDOW_UPDATE_DELAY)
    current_window_min = FLOW_WINDOW_MINUTES
    prev_window_min = current_window_min
    last_large_neg_flow_ts = None
    last_large_neg_flow_window = None

    for idx, row in tqdm(df.iterrows(), total=len(df), desc=f"Flow fit {tank_label}"):
        if row["is_outlier"] or pd.isna(row["gallons"]):
            continue
        ts = row["timestamp"]

        # Short-term flow used to steer the adaptive window.
        short_mask = (
            (~df["is_outlier"])
            & (~df["gallons"].isna())
            & (df["timestamp"] >= ts - short_window)
            & (df["timestamp"] <= ts)
        )
        short_df = df.loc[short_mask, ["timestamp", "gallons"]]
        short_flow = np.nan
        if len(short_df) >= 2:
            times_ns = short_df["timestamp"].astype("int64")
            t0 = times_ns.iloc[0]
            hours = (times_ns - t0) / 3.6e12
            if not np.all(hours == 0):
                slope, _ = np.polyfit(hours, short_df["gallons"], 1)
                short_flow = float(slope)
                flow_history.append(short_flow)

        # Decide target window based on recent short flows.
        target_window = current_window_min
        if flow_history:
            abs_all_lt = all(abs(v) < LOW_FLOW_THRESHOLD for v in flow_history)
            all_neg = all(v < 0 for v in flow_history)
            all_pos = all(v > 0 for v in flow_history)
            if abs_all_lt:
                target_window = ADAPT_POS_MAX_WINDOW_MIN
            elif all_neg:
                target_window = ADAPT_NEG_WINDOW_MIN
            elif all_pos:
                mean_flow = float(np.mean(flow_history))
                target_window = max(1200.0 / max(mean_flow, 1e-6), ADAPT_POS_MAX_WINDOW_MIN)
            else:
                target_window = current_window_min  # mixed signs, hold window

        # Gradually move window toward target.
        current_window_min = current_window_min + (target_window - current_window_min) * ADAPT_STEP
        current_window_min = max(current_window_min, 0.1)  # avoid zero/negative

        # Compute flow using trailing adaptive window, with guard against expanding past last large negative flow.
        proposed_window_min = current_window_min
        trailing_window_min = proposed_window_min
        if (
            last_large_neg_flow_ts is not None
            and proposed_window_min > prev_window_min
            and (ts - pd.Timedelta(minutes=proposed_window_min)) < last_large_neg_flow_ts
        ):
            delta_minutes = max(0.0, (ts - last_large_neg_flow_ts).total_seconds() / 60.0)
            guard_window = last_large_neg_flow_window if last_large_neg_flow_window is not None else 0.0
            max_allowed = max(guard_window, delta_minutes)
            trailing_window_min = min(proposed_window_min, max_allowed)

        trailing_window = pd.Timedelta(minutes=trailing_window_min)
        window_mask = (
            (~df["is_outlier"])
            & (~df["gallons"].isna())
            & (df["timestamp"] >= ts - trailing_window)
            & (df["timestamp"] <= ts)
        )
        window_df = df.loc[window_mask, ["timestamp", "gallons"]]
        if len(window_df) < 2:
            continue

        times_ns = window_df["timestamp"].astype("int64")
        t0 = times_ns.iloc[0]
        hours = (times_ns - t0) / 3.6e12
        if np.all(hours == 0):
            continue
        slope, _ = np.polyfit(hours, window_df["gallons"], 1)
        df.at[idx, "flow_gph"] = float(slope)
        df.at[idx, "flow_window_min"] = trailing_window_min

        if slope < -LARGE_NEG_FLOW_GPH:
            last_large_neg_flow_ts = ts
            last_large_neg_flow_window = trailing_window_min

        prev_window_min = trailing_window_min

    return df


def plot_tank(df: pd.DataFrame, tank_name: str) -> Optional[Path]:
    if df.empty:
        return None

    inliers = df[~df["is_outlier"]]
    outliers = df[df["is_outlier"]]

    fig, axes = plt.subplots(4, 1, figsize=(14, 12), sharex=True)

    axes[0].scatter(inliers["timestamp"], inliers["depth"], s=4, alpha=0.5, color="C0", label="depth (inlier)")
    if not outliers.empty:
        axes[0].scatter(outliers["timestamp"], outliers["depth"], s=4, alpha=0.5, color="C3", label="Hampel outlier")
    axes[0].set_ylabel("Depth (in)")
    axes[0].legend()
    axes[0].grid(True, linestyle="--", alpha=0.6)

    axes[1].plot(inliers["timestamp"], inliers["gallons"], color="C1", linestyle="-", label="gallons")
    axes[1].set_ylabel("Volume (gal)")
    axes[1].legend()
    axes[1].grid(True, linestyle="--", alpha=0.6)

    axes[2].plot(
        inliers["timestamp"],
        inliers["flow_gph"],
        color="C2",
        linestyle="-",
        label=f"flow (adaptive trailing window, init {FLOW_WINDOW_MINUTES} min)",
    )
    axes[2].set_ylabel("Flow (gal/hr)")
    axes[2].legend()
    axes[2].grid(True, linestyle="--", alpha=0.6)

    axes[3].plot(
        inliers["timestamp"],
        inliers["flow_gph"],
        color="C2",
        linestyle="-",
        label=f"flow (adaptive trailing window, init {FLOW_WINDOW_MINUTES} min, clipped)",
    )
    axes[3].set_ylabel("Flow (gal/hr)")
    axes[3].set_xlabel("Timestamp")
    axes[3].set_ylim(-600, 200)
    axes[3].legend()
    axes[3].grid(True, linestyle="--", alpha=0.6)

    fig.suptitle(f"{tank_name} Hampel-filtered depth, gallons, and flow", fontsize=14)
    fig.autofmt_xdate()
    fig.tight_layout(rect=[0, 0.03, 1, 0.97])

    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PLOT_DIR / f"{tank_name}_hampel_flow.png"
    fig.savefig(out_path, bbox_inches="tight", dpi=150)
    plt.close(fig)
    return out_path


def process_tank(tank_name: str) -> Tuple[pd.DataFrame, Optional[Path], Path]:
    df = load_tank_data(tank_name)
    df = apply_hampel_depth(df)
    df = compute_flow(df)

    plot_path = plot_tank(df, tank_name)

    output_cols = ["timestamp", "depth", "is_outlier", "gallons", "flow_gph"]
    output_path = OUTPUT_CSV_DIR / f"{tank_name}.csv"
    df.to_csv(output_path, columns=output_cols, index=False)
    return df, plot_path, output_path


def main() -> None:
    for tank_name in tqdm(TVF.tank_names, desc="Processing tanks"):
        df, plot_path, csv_path = process_tank(tank_name)
        if df.empty:
            print(f"[{tank_name}] No data found in {DATASET_DIR}")
            continue
        print(f"[{tank_name}] rows: {len(df)}")
        if plot_path:
            print(f"[{tank_name}] plot saved to {plot_path}")
        print(f"[{tank_name}] data saved to {csv_path}")


if __name__ == "__main__":
    main()
