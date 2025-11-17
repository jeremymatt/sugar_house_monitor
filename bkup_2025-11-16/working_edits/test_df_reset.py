import datetime as dt
import pandas as pd
import os
import re

df_col_order = ['datetime','col1','col2','col3']

def make_df(df_col_order=df_col_order):
    history_df = pd.DataFrame()
    now = dt.datetime.now()
    for i in range(10):
        history_df.loc[i,df_col_order] = [now-dt.timedelta(days=i/4),i,i*2,i*3]

    return history_df


def reset_dataframe(history_df,days_back,df_col_order=df_col_order):
    cur_time = dt.datetime.now()
    old_data = history_df[history_df.datetime<(cur_time-dt.timedelta(days=days_back))]
    data_store_directory = os.getcwd()
    name = 'TEST'
    pat = '_...csv'
    if len(old_data)>0:
        min_date = min(old_data.datetime)
        fn = os.path.join(data_store_directory,'{}_{}-{}-{}.csv'.format(name,min_date.year,str(min_date.month).zfill(2),str(min_date.day).zfill(2)))
        ctr = 1
        while os.path.isfile(fn):
            base = fn.split('.csv')[0]
            if ctr>1:
                parts = base.split('_')
                base = '_'.join(parts[:-1])
            fn = '{}_{}.csv'.format(base,str(ctr).zfill(2))
            ctr += 1
        old_data[df_col_order[1:]].to_csv(fn)
        history_df = history_df[history_df.datetime>=(cur_time-dt.timedelta(days=days_back))] 

    return (history_df,old_data)


fn = 'TEST_2025-04-03_1.csv'