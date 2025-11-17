import pandas as pd
import numpy as np
import datetime as dt
import os
import time
from multiprocessing import Queue
import adafruit_character_lcd.character_lcd_rgb_i2c as character_lcd
import board
import busio
import RPi.GPIO as GPIO
import serial
from hampel import hampel
from pathlib import Path

scripts_dir = Path(__file__).parent.resolve()
#Set the GPIO pin numbering mode
GPIO.setmode(GPIO.BCM)

df_col_order = ['datetime','timestamp','yr','mo','day','hr','m','s','surf_dist','depth','gal']

tank_names = ['brookside','roadside']

queue_dict = {}
for tank_name in tank_names:
    queue_dict[tank_name] = {}
    queue_dict[tank_name]['command'] = Queue()
    queue_dict[tank_name]['response'] = Queue()
    queue_dict[tank_name]['screen_response'] = Queue()


queue_dict['brookside']['uart'] = serial.Serial("/dev/serial0", baudrate=9600, timeout=0.5)
queue_dict['roadside']['uart'] = serial.Serial("/dev/ttyAMA5", baudrate=9600, timeout=0.5)


lcd_red = [100,0,0]
lcd_off = [0,0,0]


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

def calculate_checksum(buffer):
    # Ensures that the checksum is constrained to a single byte (8 bits)
    return (buffer[0] + buffer[1] + buffer[2]) & 0xFF

brookside_length = 191.5
brookside_width = 48
brookside_height = 40.75
brookside_radius = 17
brookside_depths = [0,1,2,3,4,5,6,7,8,9,10,11,12,13.25,20,25,30,36.5] #inches
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
tank_dims_dict['brookside']['bottom_dist'] = 55.125 #inches from the sensor to the bottom of the tank
tank_dims_dict['roadside'] = {}
tank_dims_dict['roadside']['length'] = roadside_length
tank_dims_dict['roadside']['width'] = roadside_width
tank_dims_dict['roadside']['height'] = roadside_height
tank_dims_dict['roadside']['radius'] = roadside_radius
tank_dims_dict['roadside']['dim_df'] = roadside_dimension_df
tank_dims_dict['roadside']['bottom_dist'] = 50.75 #inches from the sensor to the bottom of the tank

data_store_directory = scripts_dir.parent / 'data'
data_store_directory.mkdir(parents=True, exist_ok=True)
    

def init_display():        
    lcd_columns = 16
    lcd_rows = 2
    i2c = busio.I2C(board.SCL, board.SDA)
    lcd = character_lcd.Character_LCD_RGB_I2C(i2c, lcd_columns, lcd_rows)
    lcd.color = lcd_red
    return lcd

def run_lcd_screen(lcd,queue_dict):
    lcd.clear()
    lcd.message = "HELLO"
    time.sleep(2)
    lcd.clear()

    next_update = dt.datetime.now() - dt.timedelta(days=1)
    screen_update_frequency = dt.timedelta(seconds=15)

    tank_ctr = 0
    param_ctr = 0
    prev_msg = 'none'
    cur_msg = 'weekend'

    while True:
        now = dt.datetime.now()
        if now > next_update:
            next_update = now+screen_update_frequency
            responses = {}
            for ind,tank_name in enumerate(tank_names):
                queue_dict[tank_name]['command'].put('update_screen')
                while queue_dict[tank_name]['screen_response'].empty():
                    time.sleep(0.1)
                responses[ind] = queue_dict[tank_name]['screen_response'].get()

        if tank_ctr in responses.keys():
            if param_ctr in responses[tank_ctr].keys():
                cur_msg = '{}\n{}'.format(responses[tank_ctr]['name'],responses[tank_ctr][param_ctr])
            else:
                print('{} param ctr not in keys: {}'.format(param_ctr,responses[tank_ctr].keys()))
        else:
            print('{} not in keys: {}'.format(tank_ctr,responses.keys()))
        if not cur_msg == prev_msg:
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

def run_tank_controller(tank_name,queue_dict,measurement_rate_params):
    num_to_average,delay,readings_per_min,window_size,n_sigma,rate_update_dt = measurement_rate_params
    reading_wait_time = dt.timedelta(seconds=60/readings_per_min)
    command_queue = queue_dict[tank_name]['command']
    response_queue = queue_dict[tank_name]['response']
    screen_response_queue = queue_dict[tank_name]['screen_response']
    uart = queue_dict[tank_name]['uart']
    tank = TANK(tank_name,uart,num_to_average,delay,window_size,n_sigma,rate_update_dt)
    update_time = dt.datetime.now()-dt.timedelta(days=1)
    while True:
        now = dt.datetime.now()
        if now>update_time:
            update_time = now+reading_wait_time
            tank.update_status()
        # Check for commands from the main process
        if not command_queue.empty():
            command = command_queue.get()
            parts = command.split(':')
            if len(parts)==2:
                command = parts[0]
                command_val = int(parts[1])
            allowable_commands = [
                'update',
                'update_screen',
                'set_mins_back'
            ]
            if command in allowable_commands:
                if command == 'update_screen':
                    tank.get_tank_rate()
                    screen_response_queue.put(tank.return_screen_data())
                else:
                    if command == "update":
                        tank.get_tank_rate()
                    if command == "set_mins_back":
                        tank.update_mins_back(command_val)
                        tank.get_tank_rate()
                    response_queue.put(tank.return_current_state())


class TANK:
    def __init__(self,tank_name,uart,num_to_average,delay,window_size,n_sigma,rate_update_dt,tank_dims_dict=tank_dims_dict):
        self.name = tank_name
        self.uart = uart
        self.num_to_average = num_to_average
        self.delay = delay
        self.window_size = window_size
        self.n_sigma = n_sigma
        self.rate_update_dt = dt.timedelta(seconds=rate_update_dt)
        self.next_rate_update = dt.datetime.now()-dt.timedelta(days=1)
        self.uart_trigger = 0x55
        self.length = tank_dims_dict[tank_name]['length']
        self.width = tank_dims_dict[tank_name]['width']
        self.height = tank_dims_dict[tank_name]['height']
        self.radius = tank_dims_dict[tank_name]['radius']
        self.dim_df = tank_dims_dict[tank_name]['dim_df']
        self.bottom_dist = tank_dims_dict[tank_name]['bottom_dist']
        self.mins_back = 30
        self.filling = False
        self.emptying = False

        self.output_fn = os.path.join(data_store_directory,'{}.csv'.format(tank_name))

        if os.path.isfile(self.output_fn):
            self.history_df = pd.read_csv(self.output_fn)
            self.history_df.set_index('Unnamed: 0',inplace=True,drop=True)
            self.history_df['datetime'] = pd.to_datetime(self.history_df['timestamp'])
            self.history_df = self.history_df[df_col_order]
        else:
            self.history_df = pd.DataFrame()


        self.current_day = dt.datetime.now().day

    def read_distance(self):
        self.uart.write(bytes([self.uart_trigger]))
        time.sleep(0.1)
        Distance = None
        if self.uart.in_waiting > 0:
            time.sleep(0.004)
            if self.uart.read(1) == b'\xff':  # Judge packet header
                buffer_RTT = self.uart.read(3)
                if len(buffer_RTT) == 3:
                    CS = calculate_checksum(b'\xff' + buffer_RTT)
                    if buffer_RTT[2] == CS:
                        Distance = (buffer_RTT[0] << 8) + buffer_RTT[1]  # Calculate distance

        return Distance
    
    def get_average_distance(self):
        cur_readings = []
        for i in range(self.num_to_average):
            distance = self.read_distance()
            if not isinstance(distance,type(None)):
                distance /= 25.4
                distance = np.round(distance,2)
                cur_readings.append(distance)

            time.sleep(self.delay)
        
        if len(cur_readings)>0:
            self.dist_to_surf = np.mean(cur_readings)
        else:
            self.dist_to_surf = None

    def update_status(self):
        self.get_average_distance()

        if self.current_day != dt.datetime.now().day:
            cur_time = dt.datetime.now()
            self.current_day = cur_time.day

            old_data = self.history_df[self.history_df.datetime<(cur_time-dt.timedelta(days=1))]
            if len(old_data)>0:
                min_date = min(old_data.datetime)
                fn = os.path.join(data_store_directory,'{}_{}_{}_{}.csv'.format(self.name,min_date.year,str(min_date.month).zfill(2),str(min_date.day).zfill(2)))
                old_data[df_col_order[1:]].to_csv(fn)
                self.history_df = self.history_df[self.history_df.datetime>=(cur_time-dt.timedelta(days=1))]  

        if isinstance(self.dist_to_surf,type(None)):
            print('ERROR ({} at {}): No distance measurement')
            self.error_state = 'Invalid distance measurement'
            self.status_message = 'ERR:no dist meas'
        else:
            self.get_gal_in_tank()
            ts = dt.datetime.now()
            ind = len(self.history_df)
            row_data = [pd.to_datetime(ts),str(ts),ts.year,ts.month,ts.day,ts.hour,ts.minute,ts.second,self.dist_to_surf,self.depth,self.current_gallons]
            self.history_df.loc[ind,df_col_order] = row_data
            self.history_df[df_col_order[1:]].to_csv(self.output_fn)

    def update_mins_back(self,mins_back):
        self.mins_back += mins_back
        self.mins_back = max([5,self.mins_back])
        self.mins_back = min([240,self.mins_back])

    def get_tank_rate(self):
        now = dt.datetime.now()
        if now > self.next_rate_update:
            self.next_rate_update = now + self.rate_update_dt
            hampel_unfiltered = int(self.window_size/2)+1

            if len(self.history_df)<hampel_unfiltered+10:
                self.filling = False
                self.emptying = False
                self.remaining_time = 'not enough data'
                self.tank_rate = 'ND'
            else:
                result = hampel(self.history_df.gal,window_size = self.window_size,n_sigma=float(self.n_sigma))
                self.history_df['gal_filter'] = result.filtered_data
                temp_df = self.history_df[:-hampel_unfiltered].copy()
                rate_window_lim = temp_df.loc[temp_df.index[-1],'datetime']-dt.timedelta(minutes=self.mins_back)
                rate_window = temp_df.loc[temp_df.datetime>rate_window_lim]

                if len(rate_window)<5:
                    self.tank_rate = "ND"
                else:
                    timedelta = rate_window.datetime.diff()
                    d_hrs = [val.total_seconds()/3600 for val in timedelta[timedelta.index[1:]]]
                    d_hrs.insert(0,0)
                    poly = np.polyfit(np.cumsum(d_hrs),rate_window.gal_filter,1)

                    self.filling = False
                    self.emptying = False
                    if np.isnan(poly[0]):
                        self.tank_rate = 'nan'
                        self.history_df.to_csv('./data/nan.csv')
                        print(rate_window)
                        print(d_hrs)
                    else:
                        self.tank_rate = np.round(poly[0],1)
                        if self.tank_rate > 5:
                            self.filling = True
                        elif self.tank_rate < -5:
                            self.emptying = True

                    self.remaining_time = 'N/A'
                    if self.filling:
                        hours = (max(self.dim_df.gals_interp)-poly[1])/poly[0]-sum(d_hrs)
                        self.remaining_time = dt.timedelta(hours=hours)
                        self.remaining_time = dt.timedelta(seconds=self.remaining_time.seconds)
                    if self.emptying:
                        hours = (0-poly[1])/poly[0]-sum(d_hrs)
                        self.remaining_time = dt.timedelta(hours=hours)
                        self.remaining_time = dt.timedelta(seconds=self.remaining_time.seconds)

    def return_screen_data(self):
        state = {}
        state['name'] = self.name
        state[0] = '{} | {}gph'.format(int(self.current_gallons),self.tank_rate,1)
        state[1] = 'raw dst: {}"'.format(self.dist_to_surf)
        state[2] = 'depth: {}"'.format(self.depth)
        return state

    def return_current_state(self):
        state = {}
        state['name'] = self.name
        state['current_gallons'] = str(np.round(self.current_gallons,0))
        state['rate']  = str(self.tank_rate)
        state['filling'] = str(self.filling)
        state['emptying'] = str(self.emptying)
        state['rate_str'] = '---'
        state['remaining_time'] = 'N/A'
        try:
            state['dist_to_surf'] = str(np.round(self.dist_to_surf,3))
            state['depth'] = str(np.round(self.depth,3))
        except:
            state['dist_to_surf'] = '???'
            state['depth'] =  '???'

        if self.filling:
            remaining_hrs = np.round(self.remaining_time.seconds/3600,1)
            # state['remaining_time'] = 'Full in {}(hh:mm:ss)'.format(self.remaining_time)
            remaining_time_prefix = 'Full'
            state['rate_str'] = '{}gals/hr over previous {}mins'.format(self.tank_rate,self.mins_back)
        if self.emptying:
            # state['remaining_time'] = 'Empty in {}(hh:mm:ss)'.format(self.remaining_time)
            remaining_time_prefix = 'Empty'
            state['rate_str'] = '{}gals/hr over previous {}mins'.format(self.tank_rate,self.mins_back)

        if self.filling or self.emptying:
            full_time = dt.datetime.now()+self.remaining_time
            remaining_hrs = np.round(self.remaining_time.seconds/3600,1)
            state['remaining_time'] = '{} at {} ({}hrs)'.format(remaining_time_prefix, full_time.strftime('%I:%M%p'),remaining_hrs)

        state['mins_back'] = self.mins_back

        return state



    def get_gal_in_tank(self):
        depth = self.bottom_dist-self.dist_to_surf
        self.raw_depth = np.round(depth,2)
        depth = max([0,depth])
        depth = min([depth,self.dim_df['depths'].max()])
        self.depth = depth

        print('{}:\nbottom_dist: {}\ndist_reading: {}\nraw_depth: {}\nadjusted_depth:{}\n'.format(self.name,self.bottom_dist,self.dist_to_surf,self.raw_depth,depth))

        ind = self.dim_df.loc[self.dim_df['depths']<=depth].index[-1]
        bottom_depth = self.dim_df.loc[ind,'depths']
        gallons = self.dim_df.loc[ind,'gals_interp']

        if depth > bottom_depth:
            bottom_width = self.dim_df.loc[ind,'widths']
            top_width = np.interp(depth,self.dim_df['depths'],self.dim_df['widths'])
            vol = self.length*(depth-bottom_depth)*(bottom_width+top_width)/2
            gallons += vol/231

        self.current_gallons = np.round(gallons,2)