import pandas as pd
from scipy.optimize import curve_fit
from tqdm import tqdm
import numpy as np
import datetime as dt
import matplotlib.pyplot as plt
from pathlib import Path
import os

output_dir = Path(os.getcwd()) / 'plots'


tank = 'brookside'
df = pd.read_csv(f'{tank}.csv')
df['timestamp'] = pd.to_datetime(df['timestamp'])
df['gallons'] = np.nan
df['flow_gph'] = np.nan

window_size = 50

in2gal = {
    'brookside':191.5*48*0.004329, #gal/in
    'roadside':176.5*54*0.004329 #gal/in
    }

def line_func(x,m,b):
    return m*x+b

def quad_func(x,a,b,c):
    return a*x**2+b*x+c

cur_breakpoint = 0
break_up_thresh = 1
break_down_thresh = -1
max_window = 200 #minutes
min_window = 20 #minutes
look_ahead = dt.timedelta(minutes=1)
look_ahead_gap = dt.timedelta(minutes=1)
cur_window = dt.timedelta(minutes=min_window)

df['ts_ms'] = df['timestamp'].astype('int64')/1e9

breakpoints = []
is_break_up = []
i=400

prev_break = None

for i in tqdm(range(12,len(df)),desc="smoothing: "):
    ts,depth,is_outlier,gal,flow,ts_ms = df.loc[i]
    if is_outlier:
        continue
    prev_is_outlier = True
    di = 1
    while prev_is_outlier:
        prev_ts,prev_depth,prev_is_outlier,prev_gals,prev_flow,prev_ts_ms = df.loc[i-di]
        di+=1
    delta_t = ts-prev_ts     #nanoseconds
    delta_t = delta_t.value/1e9   #seconds
    delta_t = delta_t/3600        #hours
    window = df.loc[(df['timestamp']>=ts-cur_window)&(df['timestamp']<=ts)&~df['is_outlier']]
    params,cov = curve_fit(quad_func, window['ts_ms'], window['depth'])
    a,b,c = params
    cur_gals = quad_func(ts_ms,a,b,c)*in2gal[tank]
    delta_gals = cur_gals-prev_gals
    df.loc[i,'flow_gph'] = delta_gals/delta_t
    df.loc[i,'gallons'] = cur_gals
    forward_window = df.loc[(df['timestamp']>=ts+look_ahead_gap)&(df['timestamp']<=ts+look_ahead+look_ahead_gap)&~df['is_outlier']]
    pred_diffs = forward_window['depth'] - quad_func(forward_window['ts_ms'],a,b,c)
    break_up = np.all(pred_diffs>break_up_thresh)
    break_down = np.all(pred_diffs<break_down_thresh)

    breakpoint = False

    if break_up:
        if cur_breakpoint in window.index:
            if (prev_break != 'up'):
                breakpoint = True
                prev_break = 'up'
        else:
            breakpoint = True
            prev_break = 'up'
    if break_down:
        if cur_breakpoint in window.index:
            if (prev_break != 'down'):
                breakpoint = True
                prev_break = 'down'
        else:
            breakpoint = True
            prev_break = 'down'


    if breakpoint:
        breakpoints.append(ts)
        is_break_up.append(prev_break=='up')
        cur_breakpoint = i

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

non_outlier_df = df.loc[~df['is_outlier']]


axes[1].plot(
    non_outlier_df["timestamp"],
    non_outlier_df["gallons"],
    color="C4",
    linewidth=1.0,
    linestyle="--"
)
axes[1].set_ylabel("sap gals")
axes[1].grid(True, linestyle="-", linewidth=0.5, alpha=0.7)

ylims = [non_outlier_df["flow_gph"].min(),non_outlier_df["flow_gph"].max()]
breakpoint_colors = np.where(is_break_up,'blue','red')
for breakpoint,color in list(zip(breakpoints,breakpoint_colors)):
    axes[2].plot([breakpoint,breakpoint],ylims,linestyle="-",color=color,linewidth=1,alpha=.5)


axes[2].plot(
    non_outlier_df["timestamp"],
    non_outlier_df["flow_gph"],
    color="C4",
    linewidth=1.0,
    linestyle="--"
)
axes[2].set_ylabel("flow(gph)")
axes[2].grid(True, linestyle="-", linewidth=0.5, alpha=0.7)
axes[2].set_xlabel("timestamp")


print('Found {} breakpoints'.format(len(breakpoints)))

fig.autofmt_xdate()
fig.tight_layout()

output_dir.mkdir(parents=True, exist_ok=True)
out_path = output_dir / f"{tank}_linear-smooth.png"
fig.savefig(out_path, bbox_inches="tight", dpi=150)
plt.close(fig)