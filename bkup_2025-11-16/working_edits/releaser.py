#!/usr/bin/env python
import subprocess
import pifacedigitalio
import time
from time import sleep

DELAY = 1.0 # seconds
count = 0
pump_start_time = 0
pump_stop_time = 0
pump_interval = 0
vacuum_status = 'NA'
pfd = pifacedigitalio.PiFaceDigital()
pifacedigitalio.init()
def tank_full(event):
    if pfd.leds[0].value == 0:
        global pump_start_time
        if pump_start_time == 0:
            pump_start_time = time.time()
        if pfd.input_pins[1].value == 1:
            pfd.leds[0].turn_on() #Turn on Releaser pump
            f = open('/home/pi/releaser/pump_times.csv',"a")
            f.write(time.strftime("%Y-%m-%d-%X,")+'Auto Pump Start,'+',,\n')
            f.close()
        else:
            f = open('/home/pi/releaser/error.log',"a")
            f.write(time.strftime("%y-%m-%d-%X,")+'Auto start when tank was not empty \n')
            f.close()
def manual_pump(event):
    global pump_start_time
    global vacuum_status 
    vacuum_status = 'ON'
    pfd.leds[1].turn_on()  #Turn on Vacuum pump
    if pump_start_time == 0:
        pump_start_time = time.time()
    if pfd.input_pins[1].value == 1:
        pfd.leds[0].turn_on() #Turn on Releaser Pump
        f = open('/home/pi/releaser/pump_times.csv',"a")
        f.write(time.strftime("%Y-%m-%d-%X,")+'Manual Pump Start,'+',,\n')
        f.close()
    else:
        f = open('/home/pi/releaser/error.log',"a")
        f.write(time.strftime("%Y-%m-%d-%X,")+'Manual start when tank was not empty \n')
        f.close       
def tank_empty(event):
    if pfd.leds[0].value == 1:
        pfd.leds[0].turn_off()
        global pump_start_time
        global pump_stop_time
        global pump_interval
        global vacuum_status
        pump_run_time = 0
        pump_interval = time.time() - pump_stop_time
        fill_time = pump_start_time - pump_stop_time
        flow_rate = (12.18/fill_time)*3600
        pump_stop_time = time.time()
        if pump_interval > 1800:
            pfd.leds[1].turn_off() # Turn off Vacuum pump if pump interval is greater than 30 min   
            vacuum_status = 'Off'
        if pump_start_time != 0:
            pump_run_time = pump_stop_time - pump_start_time
        else:
            pump_run_time = 0
        f = open('/home/pi/releaser/pump_times.csv',"a")
        f.write(time.strftime("%Y-%m-%d-%X,")+'Pump Stop,'+str(round(pump_run_time,2))+','+str(round(pump_interval/60,2))+','+str(round(flow_rate,2))+'\n')
        f.close()
        pump_start_time = 0
        f = open('/home/pi/releaser/index.html',"w")
        f.write('<!DOCTYPE html PUBLIC "-//IETF//DTD HTML 2.0//EN"> \n <HTML> \n<HEAD>\n <TITLE> \n Sap Releaser \n </TITLE> \n </HEAD> \n <BODY>\n  <P>Pumped for '+str(pump_run_time)+' Sec At '+time.strftime("%Y-%m-%d, %X,")+' Time between pumps: '+str(pump_interval/60)+' Vacuum pump is '+vacuum_status+'</P> \n </BODY> \n </HTML>')
        f.close()
        try:
            subprocess.call("./uploadtest.sh", shell=True)
        except Exception, e:
            print ("failed to upload: %s" % e)
            f = open('/home/pi/releaser/error.log',"a")
            f.write(time.strftime("%Y-%m-%d-%X,")+'failed to upload \n')
            f.close()

if __name__ == "__main__":
    pfd.leds[2].turn_on()   #Turn on LED to show program is running
    listener = pifacedigitalio.InputEventListener(chip=pfd)
    listener.register(0, pifacedigitalio.IODIR_ON, tank_full)
    listener.register(1, pifacedigitalio.IODIR_OFF, tank_empty)
    listener.register(2, pifacedigitalio.IODIR_ON, manual_pump)
    listener.activate()
    


