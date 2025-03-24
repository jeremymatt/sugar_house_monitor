import pandas as pd
import matplotlib.pyplot as plt
import os
from scipy.signal import savgol_filter
from hampel import hampel

data_fn = os.path.expanduser('~/sugar_house_monitor/data/brookside_working2.csv')

df = pd.read_csv(data_fn)
#2025-03-15 11:11:00.557335
df['datetime'] = pd.to_datetime(df['timestamp'])

plot_col = 'depth'

result = hampel(df[plot_col],window_size = 100,n_sigma=3.0)

filtered_df = df.loc[result.filtered_data.index]


fig,ax = plt.subplots(1,1,sharex=True,figsize=[20,10])
ax.plot(df['datetime'],result.filtered_data,linestyle='None',marker='o',label='filtered_{}'.format(plot_col))
ax.plot(df.loc[result.outlier_indices,'datetime'],df.loc[result.outlier_indices,plot_col],linestyle='None',marker='x')


fig.legend(loc='upper right',bbox_to_anchor = (.95,.95))

fig.savefig(os.path.expanduser('~/sugar_house_monitor/plot.png'),bbox_inches='tight')




