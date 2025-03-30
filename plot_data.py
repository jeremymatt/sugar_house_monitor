import pandas as pd
import matplotlib.pyplot as plt
import os
# from scipy.signal import savgol_filter
from hampel import hampel

cd = os.getcwd()
data_path = os.path.join(os.getcwd(),'data')

data_fn = 'brookside_working_complete.csv'

# data_fn = os.path.expanduser('~/sugar_house_monitor/data/brookside_working2.csv')
data_fn = os.path.expanduser(os.path.join(data_path,data_fn))

df = pd.read_csv(data_fn)
#2025-03-15 11:11:00.557335
df['datetime'] = pd.to_datetime(df['timestamp'])

plot_col = 'depth'

n_sigma = .25
window_size = 50
result = hampel(df[plot_col],window_size = window_size,n_sigma=float(n_sigma))
# result2 = hampel(result.filtered_data,window_size = window_size,n_sigma=float(n_sigma))
# result3 = hampel(result2.filtered_data,window_size = window_size,n_sigma=float(n_sigma))

filtered_df = df.loc[result.filtered_data.index]


fig,ax = plt.subplots(1,1,sharex=True,figsize=[300,25])
ax.plot(df['datetime'],result.filtered_data,linestyle='None',marker='o',label='filtered_{}'.format(plot_col))
ax.plot(df.loc[result.outlier_indices,'datetime'],df.loc[result.outlier_indices,plot_col],linestyle='None',marker='x',label="first_filter")
# ax.plot(df.loc[result2.outlier_indices,'datetime'],result.filtered_data[result2.outlier_indices],linestyle='None',marker='+',label="second_filter")
ax.grid(which='major', color='grey', linewidth=1)
ax.grid(which='minor', color='lightgrey', linestyle='--', linewidth=0.8)
ax.minorticks_on()

fig.legend()

fig.savefig(os.path.join(cd,'plot.png'),bbox_inches='tight')






