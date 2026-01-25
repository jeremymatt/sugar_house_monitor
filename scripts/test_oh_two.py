import os
import time
import busio
import digitalio
import board
import adafruit_mcp3xxx.mcp3008 as MCP
from adafruit_mcp3xxx.analog_in import AnalogIn
import RPi.GPIO as GPIO
# import pandas as pd
import numpy as np

GPIO.setmode(GPIO.BCM)

#set pin 13 as the output
output_pin = 23
GPIO.setup(output_pin,GPIO.OUT)
GPIO.output(output_pin,GPIO.LOW)

#set voltage conversion constants
adc_reference_voltage = 5
adc_voltage_range = [0,adc_reference_voltage]
adc_value_range = [0,65535]

# create the spi bus
spi = busio.SPI(clock=board.SCK, MISO=board.MISO, MOSI=board.MOSI)

# create the cs (chip select)
cs = digitalio.DigitalInOut(board.D5)

# create the mcp object
mcp = MCP.MCP3008(spi, cs)

# create an analog input channel on pin 0
oh_two_chan = AnalogIn(mcp, MCP.P0)

last_o2 = 0       # this keeps track of the last potentiometer value
tolerance = 250     # to keep from being jittery we'll only change
                   # volume when the pot has moved a significant amount
                   # on a 16-bit ADC


#load calibration values
cal_data = np.loadtxt('oh_two_cal.csv', delimiter=',', dtype=float, skiprows=1)

def remap_range(value, left_min, left_max, right_min, right_max):
   # this remaps a value from original (left) range to new (right) range
   # Figure out how 'wide' each range is
   left_span = left_max - left_min
   right_span = right_max - right_min

   # Convert the left range into a 0-1 range (int)
   valueScaled = (value - left_min) / left_span

   # Convert the 0-1 range into a value in the right range.
   return (right_min + (valueScaled * right_span))

while True:
   # read the analog pin
   oh_two_raw = oh_two_chan.value

   # how much has it changed since the last read?
   vac_change = abs(oh_two_raw - last_o2) > tolerance

   if vac_change:
       # convert 16bit adc0 (0-65535) trim pot read into 0-5volt level
       oh_two_voltage = np.interp(oh_two_raw,adc_value_range,adc_voltage_range)
       oh_two_percent = np.interp(oh_two_voltage,cal_data[:,0],cal_data[:,1])

       # print voltage
       print('Current State = {}raw, {:0.3f}v, {:0.3f}o2'.format(oh_two_raw,oh_two_voltage,oh_two_percent))

       # save the potentiometer reading for the next loop
       last_o2 = oh_two_raw

   # hang out and do nothing for a half second
   time.sleep(0.05)