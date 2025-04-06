#!/user/bin/env python

"""
Created on Fri Dec 10 11:34:53 2021

@author: jmatt
"""

# import traceback
from flask import Flask, session, request, redirect, jsonify, render_template
from multiprocessing import Process, Queue
import time
import subprocess
# import signal
import sys
# import hashlib
import web_app as WA
import tank_vol_fcns as TVF
import serial

# app = Flask(__name__)
# app.secret_key = 'your_secret_key_here'  # Replace with a strong secret key


# Set up the serial port for UART communication
# brookside_uart = serial.Serial("/dev/serial0", baudrate=9600, timeout=0.5)
# roadside_uart = serial.Serial("/dev/ttyAMA5", baudrate=9600, timeout=0.5)

def start_ngrok(port, static_ngrok_url):
    """Start Ngrok with the specified static URL."""
    try:
        ngrok_path = '/usr/local/bin/ngrok'
        ngrok_process = subprocess.Popen(
            [ngrok_path, "http", str(port), "--url", static_ngrok_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        print(f"Ngrok started with static URL: {static_ngrok_url}")
        # Optionally, print output for debugging
        time.sleep(5)  # Wait for Ngrok to initialize
        return ngrok_process
    except Exception as e:
        print(f"Failed to start Ngrok: {e}")
        return None
   
def cleanup(ngrok_process,tank_processes,screen_process):
    print("\nCleaning up...")
    if ngrok_process:
        ngrok_process.terminate()
        print("ngrok process terminated.")
    
    for tank_name in tank_processes.keys():
        if tank_processes[tank_name]:
            tank_processes[tank_name].terminate()
            print("{} controller process terminated.".format(tank_name))

    if screen_process:
        screen_process.terminate()
    
    sys.exit(0)

# Signal handler
def signal_handler(sig, frame):
    cleanup()


if __name__ == '__main__':
    port = 8080

    if TVF.testing:
        ngrok_static_url = 'arriving-seahorse-exotic.ngrok-free.app'
        num_to_average = 8 #measurements to average into one reading
        delay = 0.25 #delay (s) between individual measurements
        readings_per_min = 4 # number of readings to target per minute
        window_size = 5 #Hampbel filtering window
        n_sigma = .25 #threshold for hampbel filter
        rate_update_dt = 15 #seconds
    else:
        ngrok_static_url = 'amused-wired-stork.ngrok-free.app'
        num_to_average = 8 #measurements to average into one reading
        delay = 0.25 #delay (s) between individual measurements
        readings_per_min = 4 # number of readings to target per minute
        window_size = 50 #Hampbel filtering window
        n_sigma = .25 #threshold for hampbel filter
        rate_update_dt = 15 #seconds
    
    # Start Ngrok
    ngrok_process = start_ngrok(port, ngrok_static_url)
    if not ngrok_process:
        print("Ngrok failed to start. Exiting.")
        exit(1)


    measurement_rate_params = (num_to_average,delay,readings_per_min,window_size,n_sigma,rate_update_dt)

    tank_processes = {}
    for tank_name in TVF.tank_names:
        print('\nINIT {} CONTROLLER PROCESS\n'.format(tank_name))
        tank_processes[tank_name] = Process(target=TVF.run_tank_controller, args=(tank_name,TVF.queue_dict,measurement_rate_params))
        print('\nSTART {} CONTROLLER PROCESS\n'.format(tank_name))
        tank_processes[tank_name].start()

    if TVF.testing:
        screen_process = None
    else:
        lcd = TVF.init_display()
        print("\nInit screen process\n")
        screen_process = Process(target=TVF.run_lcd_screen, args=(lcd,TVF.queue_dict))
        screen_process.start()

    # Start Flask app
    try:
        print(f"Starting Flask app on port {port}...")
        WA.app.run(port=port, debug=True, use_reloader=False)
    except Exception as e:
        print(f"Failed to start Flask app: {e}")
    finally:
        cleanup(ngrok_process,tank_processes,screen_process)
    
