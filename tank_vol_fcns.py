import pandas as pd
import numpy as np
import datetime as dt
import os


def calc_gallons_interp(df,length):
    df.loc[0,'gals_interp'] = 0
    for i in df.index[1:]:
        bottom_width = df.loc[i-1,'widths']
        top_width = df.loc[i,'widths']
        bottom = df.loc[i-1,'depths']
        top = df.loc[i,'depths']
        vol = length*(top-bottom)*(top_width+bottom_width)/2
        gals = np.round(vol/231,3)
        df.loc[i,'gals_interp'] = df.loc[i-1,'gals_interp']+gals

    return df

brookside_length = 191.5
brookside_width = 48
brookside_height = 40.75
brookside_radius = 17
brookside_depths = [0,1,2,3,4,5,6,7,8,9,10,11,12,13.25,20,25,30,36.25] #inches
brookside_widths = [10,19.25,27.25,33.25,37,39.5,41.5,43,44,45.5,46.5,47.375,47.75,48,48,48,48,48]
brookside_dimension_df = pd.DataFrame({'depths':brookside_depths,'widths':brookside_widths})
brookside_dimension_df = calc_gallons_interp(brookside_dimension_df,brookside_length)

roadside_length = 178.5
roadside_width = 54
roadside_height = 39
roadside_radius = 18
roadside_depths = [0,2.75,3.75,4.75,5.75,6.75,8.75,9.75,10.75,11.75,16,20,25,30,35,39]
roadside_widths = [22,38.75,41.125,43.625,45.375,46.25,48.5,49.25,50.75,51.75,54,54,54,54,54,54]
roadside_dimension_df = pd.DataFrame({'depths':roadside_depths,'widths':roadside_widths})
roadside_dimension_df = calc_gallons_interp(roadside_dimension_df,roadside_length)

tank_dims_dict = {}
tank_dims_dict['brookside'] = {}
tank_dims_dict['brookside']['length'] = brookside_length
tank_dims_dict['brookside']['width'] = brookside_width
tank_dims_dict['brookside']['height'] = brookside_height
tank_dims_dict['brookside']['radius'] = brookside_radius
tank_dims_dict['brookside']['dim_df'] = brookside_dimension_df
tank_dims_dict['brookside']['bottom_dist'] = 56 #inches from the sensor to the bottom of the tank
tank_dims_dict['roadside'] = {}
tank_dims_dict['roadside']['length'] = roadside_length
tank_dims_dict['roadside']['width'] = roadside_width
tank_dims_dict['roadside']['height'] = roadside_height
tank_dims_dict['roadside']['radius'] = roadside_radius
tank_dims_dict['roadside']['dim_df'] = roadside_dimension_df
tank_dims_dict['roadside']['bottom_dist'] = 56

data_store_directory = os.path.join(os.path.expanduser(),'sugar_house_monitor','data')
if not data_store_directory:
    os.makedirs(data_store_directory)


class TANK:
    def __init__(self,tank_name,tank_dims_dict=tank_dims_dict):
        self.name = tank_name
        self.length = tank_dims_dict[tank_name]['length']
        self.width = tank_dims_dict[tank_name]['width']
        self.height = tank_dims_dict[tank_name]['height']
        self.radius = tank_dims_dict[tank_name]['radius']
        self.dim_df = tank_dims_dict[tank_name]['dim_df']
        self.bottom_dist = tank_dims_dict[tank_name]['bottom_dist']



        self.output_fn = os.path.join(data_store_directory,'{}.csv'.format(tank_name))

        if os.path.isfile(self.output_fn):
            self.history_df = pd.read_csv(self.output_fn)
        else:
            self.history_df = pd.DataFrame()



    def update_tank_status(self,dist_to_surf):
        self.dist_to_surf = dist_to_surf
        self.get_gal_in_tank()
        ts = dt.datetime.now()
        ind = len(self.history_df)
        row_data = [str(ts),ts.year,ts.month,ts.day,ts.hour,ts.minute,ts.second,dist_to_surf,self.depth,self.current_gallons]
        self.history_df.loc[ind,['timestamp','yr','mo','day','hr','m','s','surf_dist','depth','gal']] = row_data

    def get_gal_in_tank(self):
        depth = self.bottom_dist-self.dist_to_surf
        self.depth = depth
        if depth>self.dim_df['depths'].max():
            depth = self.dim_df['depths'].max()

        ind = self.dim_df.loc[self.dim_df['depths']<=depth].index[-1]
        bottom_depth = self.dim_df.loc[ind,'depths']
        gallons = self.dim_df.loc[ind,'gals_interp']

        if depth > bottom_depth:
            bottom_width = self.dim_df.loc[ind,'widths']
            top_width = np.interp(depth,self.dim_df['depths'],self.dim_df['widths'])
            vol = self.length*(depth-bottom_depth)*(bottom_width+top_width)/2
            gallons += vol/231

        self.current_gallons = np.round(gallons,0)