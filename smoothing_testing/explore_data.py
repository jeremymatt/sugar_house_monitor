#!/usr/bin/env python3
"""Explore historical tank readings and plot raw values + diffs.

Steps:
- Load all CSVs in the dataset directory (optionally filter by tank).
- Recompute sap depth and gallons using the same geometry in scripts/tank_vol_fcns.py.
- Calculate raw flow rate (gallons per hour) between readings.
- Save scatter plots (raw + point-to-point diff) for depth, gallons, and flow.
"""

import argparse
from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from hampel import hampel
from scipy.signal import savgol_filter
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tank_vol_fcns  # noqa: E402  # isort:skip

DATASET_DIR = Path(__file__).resolve().parent / "dataset"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "plots"
OUTPUT_CSV_DIR = Path(__file__).resolve().parent

# Flow rate bounds (gallons per hour). Points outside this window are dropped.
FLOW_BOUNDS_GPH = {
    "lower": -1000.0,
    "upper": 10000.0,
}
FLOW_SMOOTH_WINDOW = 50  # span for exponential moving average on flow plots

# Hampel + moving-average parameters for alternate filtering
HAMP_WINDOW_SIZE = 50
HAMP_N_SIGMA = 0.25
HAMP_MOVING_WINDOW = 200  # moving average window after hampel filtering


def calc_depth_and_gallons(tank_name: str, surf_dist: float):
    """Replicate tank_vol_fcns get_gal_in_tank math for a single reading."""
    dims = tank_vol_fcns.tank_dims_dict[tank_name]
    dim_df = dims["dim_df"]
    length = dims["length"]
    bottom_dist = dims["bottom_dist"]

    if pd.isna(surf_dist):
        return np.nan, np.nan

    raw_depth = bottom_dist - surf_dist
    depth = max(0.0, min(raw_depth, dim_df["depths"].max()))

    ind = dim_df.loc[dim_df["depths"] <= depth].index[-1]
    bottom_depth = dim_df.loc[ind, "depths"]
    gallons = dim_df.loc[ind, "gals_interp"]

    if depth > bottom_depth:
        bottom_width = dim_df.loc[ind, "widths"]
        top_width = np.interp(depth, dim_df["depths"], dim_df["widths"])
        vol = length * (depth - bottom_depth) * (bottom_width + top_width) / 2
        gallons += vol / 231.0

    return depth, np.round(gallons, 2)


def depth_to_gallons(tank_name: str, depth: float) -> float:
    """Convert a depth measurement to gallons using tank geometry."""
    dims = tank_vol_fcns.tank_dims_dict[tank_name]
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


def load_all_data(dataset_dir: Path, tank_filter=None) -> pd.DataFrame:
    dataset_dir = Path(dataset_dir)
    frames = []
    tank_filter = set(tank_filter) if tank_filter else None

    for path in tqdm(sorted(dataset_dir.glob("*.csv")), desc="Loading CSVs"):
        tank_name = path.stem.split("_")[0]
        if tank_filter and tank_name not in tank_filter:
            continue
        if tank_name not in tank_vol_fcns.tank_dims_dict:
            continue
        df = pd.read_csv(path)
        df = df.loc[:, ~df.columns.str.contains(r"^Unnamed")]
        df["tank"] = tank_name
        frames.append(df)

    if not frames:
        raise FileNotFoundError(
            f"No CSV files found in {dataset_dir} matching tanks "
            f"{sorted(tank_filter) if tank_filter else list(tank_vol_fcns.tank_dims_dict.keys())}"
        )

    data = pd.concat(frames, ignore_index=True)
    data["timestamp"] = pd.to_datetime(data["timestamp"])
    data["surf_dist"] = pd.to_numeric(data["surf_dist"], errors="coerce")
    data = data.sort_values(["tank", "timestamp"]).reset_index(drop=True)
    return data


def compute_depth_volume(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    for tank_name, tank_df in data.groupby("tank"):
        results = tank_df["surf_dist"].apply(lambda val: calc_depth_and_gallons(tank_name, val))
        depths, gallons = zip(*results) if len(results) else ([], [])
        data.loc[tank_df.index, "calc_depth_in"] = depths
        data.loc[tank_df.index, "calc_gallons"] = gallons
    return data


def compute_flow_and_diffs(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    data["calc_flow_gph"] = np.nan
    data["flow_dropped"] = False

    lower = FLOW_BOUNDS_GPH["lower"]
    upper = FLOW_BOUNDS_GPH["upper"]
    total_candidates = 0
    dropped = 0

    for tank_name, tank_df in data.groupby("tank"):
        last_good_idx = None
        prev_idx = None
        for idx in tqdm(tank_df.index, desc=str(tank_name)):
            if prev_idx is None:
                prev_idx = idx
                continue

            ref_idx = last_good_idx if last_good_idx is not None else prev_idx
            dt_hours = (data.loc[idx, "timestamp"] - data.loc[ref_idx, "timestamp"]).total_seconds() / 3600.0
            if dt_hours <= 0:
                print("WARNING: non-positive timedelta={}hrs for tank {}".format(dt_hours,tank_name))
                print('  Timestamp1: {}'.format(data.loc[ref_idx, "timestamp"]))
                print('  Timestamp2: {}'.format(data.loc[idx, "timestamp"]))
                prev_idx = idx
                continue

            gal_current = data.loc[idx, "calc_gallons"]
            gal_ref = data.loc[ref_idx, "calc_gallons"]
            if pd.isna(gal_current) or pd.isna(gal_ref):
                prev_idx = idx
                continue

            total_candidates += 1
            flow = (gal_current - gal_ref) / dt_hours
            if lower <= flow <= upper:
                data.loc[idx, "calc_flow_gph"] = flow
                last_good_idx = idx
            else:
                data.loc[idx, "flow_dropped"] = True
                dropped += 1
            prev_idx = idx

    for col in ["calc_depth_in", "calc_gallons", "calc_flow_gph"]:
        data[f"{col}_diff"] = data.groupby("tank")[col].diff()

    drop_fraction = dropped / total_candidates if total_candidates else 0
    drop_stats = {
        "total_candidates": total_candidates,
        "dropped": dropped,
        "drop_fraction": drop_fraction,
    }

    return data, drop_stats


def compute_hampel_flow(data: pd.DataFrame) -> pd.DataFrame:
    """Apply Hampel filter to gallons, then a moving average, and derive flow."""
    data = data.copy()
    data["hampel_depth_in"] = np.nan
    data["hampel_gallons"] = np.nan
    data["hampel_ma_gallons"] = np.nan
    data["hampel_flow_gph"] = np.nan
    data["hampel_outlier"] = False

    for tank_name, tank_df in data.groupby("tank"):
        if tank_df.empty:
            continue
        depth_series = pd.to_numeric(tank_df["calc_depth_in"], errors="coerce")
        if depth_series.isna().all():
            continue
        res = hampel(depth_series.to_numpy(), window_size=HAMP_WINDOW_SIZE, n_sigma=float(HAMP_N_SIGMA))
        filtered_arr = np.asarray(res.filtered_data)
        if filtered_arr.shape[0] != len(depth_series):
            continue
        filtered = pd.Series(filtered_arr, index=tank_df.index)
        data.loc[tank_df.index, "hampel_depth_in"] = filtered
        data.loc[tank_df.index, "hampel_gallons"] = filtered.apply(lambda d: depth_to_gallons(tank_name, d))
        if hasattr(res, "outlier_indices") and res.outlier_indices is not None:
            data.loc[tank_df.index[res.outlier_indices], "hampel_outlier"] = True

        ma = data.loc[tank_df.index, "hampel_gallons"].rolling(HAMP_MOVING_WINDOW, min_periods=1).mean()
        data.loc[tank_df.index, "hampel_ma_gallons"] = ma

        dt_hours = tank_df["timestamp"].diff().dt.total_seconds() / 3600.0
        dt_hours.loc[dt_hours <= 0] = np.nan
        flow = ma.diff() / dt_hours
        data.loc[tank_df.index, "hampel_flow_gph"] = flow

    data["hampel_flow_gph_diff"] = data.groupby("tank")["hampel_flow_gph"].diff()
    return data


def compute_savgol_depth_flow(data: pd.DataFrame) -> pd.DataFrame:
    """Smooth depth with Savitzky-Golay, then derive gallons and flow."""
    data = data.copy()
    data["savgol_depth_in"] = np.nan
    data["savgol_gallons"] = np.nan
    data["savgol_flow_gph"] = np.nan

    for tank_name, tank_df in data.groupby("tank"):
        base_depth = (
            pd.to_numeric(tank_df["hampel_depth_in"], errors="coerce")
            if "hampel_depth_in" in tank_df
            else pd.to_numeric(tank_df["calc_depth_in"], errors="coerce")
        )
        depth_series = base_depth
        if depth_series.isna().all():
            continue

        depth_filled = depth_series.interpolate(limit_direction="both")
        window = HAMP_WINDOW_SIZE if HAMP_WINDOW_SIZE % 2 == 1 else HAMP_WINDOW_SIZE + 1
        window = min(window, len(depth_filled))
        if window < 3:
            smoothed = depth_filled
        else:
            if window % 2 == 0:
                window -= 1
            polyorder = 2 if window > 2 else 1
            smoothed = savgol_filter(depth_filled, window_length=window, polyorder=polyorder)
            smoothed = pd.Series(smoothed, index=tank_df.index)

        data.loc[tank_df.index, "savgol_depth_in"] = smoothed
        data.loc[tank_df.index, "savgol_gallons"] = smoothed.apply(lambda d: depth_to_gallons(tank_name, d))

        dt_hours = tank_df["timestamp"].diff().dt.total_seconds() / 3600.0
        dt_hours.loc[dt_hours <= 0] = np.nan
        flow = data.loc[tank_df.index, "savgol_gallons"].diff() / dt_hours
        data.loc[tank_df.index, "savgol_flow_gph"] = flow

    data["savgol_flow_gph_diff"] = data.groupby("tank")["savgol_flow_gph"].diff()
    return data


def plot_depth_and_gallons(df: pd.DataFrame, tank_name: str, output_dir: Path):
    tank_df = df[df["tank"] == tank_name]
    if tank_df.empty:
        return None

    fig, ax1 = plt.subplots(figsize=(15, 6))
    depth_scatter = ax1.scatter(
        tank_df["timestamp"],
        tank_df["calc_depth_in"],
        s=6,
        alpha=0.6,
        label="sap depth (in)",
        color="C0",
    )
    ax1.set_ylabel("sap depth (in)")
    ax1.set_title(f"{tank_name} depth and gallons")

    ax2 = ax1.twinx()
    gallons_scatter = ax2.scatter(
        tank_df["timestamp"],
        tank_df["calc_gallons"],
        s=6,
        alpha=0.6,
        label="volume (gal)",
        color="C1",
    )
    ax2.set_ylabel("volume (gal)")

    fig.autofmt_xdate()
    fig.tight_layout()
    ax1.grid(True, linestyle="--", linewidth=0.5, alpha=0.7)

    # Combine legends from both axes.
    handles = [depth_scatter, gallons_scatter]
    labels = [h.get_label() for h in handles]
    ax1.legend(handles, labels, loc="upper left")

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{tank_name}_depth_gallons.png"
    fig.savefig(out_path, bbox_inches="tight", dpi=150)
    plt.close(fig)
    return out_path


def plot_depth_and_flow(df: pd.DataFrame, tank_name: str, output_dir: Path):
    tank_df = df[df["tank"] == tank_name]
    if tank_df.empty:
        return None

    flow_valid = tank_df["calc_flow_gph"].where(~tank_df["flow_dropped"])
    flow_ema = flow_valid.ewm(span=FLOW_SMOOTH_WINDOW, adjust=False, min_periods=1).mean()

    fig, axes = plt.subplots(
        3,
        1,
        figsize=(15, 9),
        sharex=True,
        gridspec_kw={"height_ratios": [1, 1, 1]},
    )

    depth_colors = np.where(tank_df["flow_dropped"], "red", "blue")
    axes[0].scatter(
        tank_df["timestamp"],
        tank_df["calc_depth_in"],
        s=6,
        alpha=0.6,
        color=depth_colors,
    )
    axes[0].set_ylabel("sap depth (in)")
    axes[0].set_title(f"{tank_name} depth and flow")
    axes[0].grid(True, linestyle="--", linewidth=0.5, alpha=0.7)

    flow_ax = axes[1]
    flow_ax.scatter(tank_df["timestamp"], flow_valid, s=6, alpha=0.6, color="C1", label="flow (gph)")
    flow_ax.plot(
        tank_df["timestamp"],
        flow_ema,
        color="C4",
        linewidth=1.0,
        linestyle="--",
        label=f"EMA (span={FLOW_SMOOTH_WINDOW})",
    )

    flow_diff_ax = axes[2]
    flow_diff_ax.scatter(tank_df["timestamp"], flow_valid.diff(), s=6, alpha=0.6, color="C2")
    flow_ax.set_ylim(-500, 500)
    flow_diff_ax.set_ylim(-500, 500)
    flow_ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.7)
    flow_diff_ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.7)

    flow_ax.set_ylabel("flow (gph)")
    flow_ax.legend(loc="upper left")

    flow_diff_ax.set_ylabel("flow diff (gph)")
    flow_diff_ax.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    flow_diff_ax.set_xlabel("timestamp")
    flow_diff_ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.7)

    fig.autofmt_xdate()
    fig.tight_layout()

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{tank_name}_depth_flow.png"
    fig.savefig(out_path, bbox_inches="tight", dpi=150)
    plt.close(fig)
    return out_path


def save_per_tank_csv(df: pd.DataFrame, output_dir: Path = OUTPUT_CSV_DIR) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for tank_name, tank_df in df.groupby("tank"):
        out_path = output_dir / f"{tank_name}.csv"
        tank_df.to_csv(out_path, index=False)


def plot_depth_and_flow_hampel(df: pd.DataFrame, tank_name: str, output_dir: Path):
    tank_df = df[df["tank"] == tank_name]
    if tank_df.empty:
        return None

    hampel_flow_valid = tank_df["hampel_flow_gph"].where(~tank_df["hampel_outlier"])

    fig, axes = plt.subplots(
        3,
        1,
        figsize=(15, 9),
        sharex=True,
        gridspec_kw={"height_ratios": [1, 1, 1]},
    )

    depth_colors = np.where(tank_df["hampel_outlier"], "red", "C0")
    axes[0].scatter(tank_df["timestamp"], tank_df["calc_depth_in"], s=6, alpha=0.6, color=depth_colors)
    axes[0].set_ylabel("sap depth (in)")
    axes[0].set_title(f"{tank_name} depth and flow (Hampel + MA)")
    axes[0].grid(True, linestyle="--", linewidth=0.5, alpha=0.7)

    flow_colors = np.where(tank_df["hampel_outlier"], "red", "C1")
    axes[1].scatter(
        tank_df["timestamp"],
        hampel_flow_valid,
        s=6,
        alpha=0.6,
        color=flow_colors,
        label="flow (gph, hampel+MA)",
    )
    axes[1].set_ylim(-500, 500)
    axes[1].grid(True, linestyle="--", linewidth=0.5, alpha=0.7)
    axes[1].set_ylabel("flow (gph)")
    axes[1].legend(loc="upper left")
    axes[2].grid(True, linestyle="--", linewidth=0.5, alpha=0.7)

    axes[2].scatter(tank_df["timestamp"], hampel_flow_valid.diff(), s=6, alpha=0.6, color="C2")
    axes[2].set_ylim(-500, 500)
    axes[2].grid(True, linestyle="--", linewidth=0.5, alpha=0.7)
    axes[2].axhline(0, color="gray", linestyle="--", linewidth=0.8)
    axes[2].set_ylabel("flow diff (gph)")
    axes[2].set_xlabel("timestamp")

    fig.autofmt_xdate()
    fig.tight_layout()

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{tank_name}_depth_flow_hampel.png"
    fig.savefig(out_path, bbox_inches="tight", dpi=150)
    plt.close(fig)
    return out_path


def plot_depth_and_flow_savgol(df: pd.DataFrame, tank_name: str, output_dir: Path):
    tank_df = df[df["tank"] == tank_name]
    if tank_df.empty:
        return None

    fig, axes = plt.subplots(
        3,
        1,
        figsize=(15, 9),
        sharex=True,
        gridspec_kw={"height_ratios": [1, 1, 1]},
    )

    axes[0].scatter(tank_df["timestamp"], tank_df["calc_depth_in"], s=6, alpha=0.6, color="C0", label="raw depth")
    axes[0].plot(
        tank_df["timestamp"],
        tank_df["savgol_depth_in"],
        color="C4",
        linewidth=1.0,
        linestyle="--",
        label="savgol depth",
    )
    axes[0].set_ylabel("sap depth (in)")
    axes[0].set_title(f"{tank_name} depth and flow (Savitzky-Golay)")
    axes[0].legend(loc="upper left")
    axes[0].grid(True, linestyle="--", linewidth=0.5, alpha=0.7)

    axes[1].scatter(
        tank_df["timestamp"],
        tank_df["savgol_flow_gph"],
        s=6,
        alpha=0.6,
        color="C1",
        label="flow (gph, savgol)",
    )
    axes[1].set_ylabel("flow (gph)")
    axes[1].set_ylim(-500, 500)
    axes[1].legend(loc="upper left")
    axes[1].grid(True, linestyle="--", linewidth=0.5, alpha=0.7)

    axes[2].scatter(
        tank_df["timestamp"],
        tank_df["savgol_flow_gph_diff"],
        s=6,
        alpha=0.6,
        color="C2",
    )
    axes[2].axhline(0, color="gray", linestyle="--", linewidth=0.8)
    axes[2].set_ylabel("flow diff (gph)")
    axes[2].set_ylim(-500, 500)
    axes[2].set_xlabel("timestamp")
    axes[2].grid(True, linestyle="--", linewidth=0.5, alpha=0.7)

    fig.autofmt_xdate()
    fig.tight_layout()

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{tank_name}_depth_flow_savgol.png"
    fig.savefig(out_path, bbox_inches="tight", dpi=150)
    plt.close(fig)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Explore historical tank readings.")
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=DATASET_DIR,
        help="Directory containing {tank}_{yyyy}_{mm}_{dd}.csv files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write plots",
    )
    parser.add_argument(
        "--tanks",
        nargs="*",
        default=None,
        help="Optional list of tank names to include (default both)",
    )
    args = parser.parse_args()

    data = load_all_data(args.dataset_dir, args.tanks)
    data = compute_depth_volume(data)
    data, drop_stats = compute_flow_and_diffs(data)
    data = compute_hampel_flow(data)
    data = compute_savgol_depth_flow(data)

    saved = []
    for tank_name in tqdm(sorted(data["tank"].unique()), desc="Plotting"):
        if tank_name not in data["tank"].unique():
            continue
        path1 = plot_depth_and_gallons(data, tank_name, args.output_dir)
        path2 = plot_depth_and_flow(data, tank_name, args.output_dir)
        path3 = plot_depth_and_flow_hampel(data, tank_name, args.output_dir)
        path4 = plot_depth_and_flow_savgol(data, tank_name, args.output_dir)
        for path in (path1, path2, path3, path4):
            if path:
                saved.append(path)

    save_per_tank_csv(data, OUTPUT_CSV_DIR)

    print(f"Loaded {len(data)} rows from {args.dataset_dir}")
    print("Plots saved:")
    for path in saved:
        print(f" - {path}")
    print(
        f"Flow points dropped by bounds: {drop_stats['dropped']} / {drop_stats['total_candidates']} "
        f"({drop_stats['drop_fraction']:.2%})"
    )


if __name__ == "__main__":
    main()
