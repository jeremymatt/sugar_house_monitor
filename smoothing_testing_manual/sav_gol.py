import pandas as pd
from scipy.signal import savgol_filter
from tqdm import tqdm
import numpy as np
import datetime as dt
import matplotlib.pyplot as plt
from pathlib import Path
import os

output_dir = Path(os.getcwd()) / 'plots'


tank = 'roadside'
df = pd.read_csv(f'{tank}.csv')
df['timestamp'] = pd.to_datetime(df['timestamp'])
df['ts_ms'] = df['timestamp'].astype('int64')/1e9

df['delta_gals'] = df['gallons'].diff()
df['delta_t'] = df['ts_ms'].diff()
df['raw_flow'] = 3600*df['delta_gals']/(df['delta_t'])

window_size = 50

non_nan_df = df.loc[~np.isnan(df['raw_flow'])]

filtered_flow = savgol_filter(non_nan_df['raw_flow'],window_size,2)

# num_filters = 10
# for i in range(num_filters):
#     filtered_flow = savgol_filter(filtered_flow,window_size,2)

fig, axes = plt.subplots(
    3,
    1,
    figsize=(15, 9),
    sharex=True,
    gridspec_kw={"height_ratios": [1, 1, 1]},
)

depth_colors = np.where(df["is_outlier"], "red", "blue")
axes[0].scatter(
    df["timestamp"],
    df["depth"],
    s=3,
    alpha=0.6,
    color=depth_colors,
)
axes[0].set_ylabel("sap depth (in)")
axes[0].set_title(f"{tank} depth and flow")
axes[0].grid(True, linestyle="--", linewidth=0.5, alpha=0.7)


axes[1].plot(
    non_nan_df["timestamp"],
    non_nan_df["raw_flow"],
    color="C4",
    linewidth=0.5,
    linestyle="-"
)
axes[1].set_ylabel("flow(gph)")
axes[1].grid(True, linestyle="-", linewidth=0.5, alpha=0.7)

axes[2].plot(
    non_nan_df["timestamp"],
    filtered_flow,
    color="C4",
    linewidth=0.5,
    linestyle="-"
)
axes[2].set_ylabel("flow(gph)")
axes[2].grid(True, linestyle="-", linewidth=0.5, alpha=0.7)
axes[2].set_xlabel("timestamp")


fig.autofmt_xdate()
fig.tight_layout()

output_dir.mkdir(parents=True, exist_ok=True)
out_path = output_dir / f"{tank}_savgol.png"
fig.savefig(out_path, bbox_inches="tight", dpi=150)
plt.close(fig)