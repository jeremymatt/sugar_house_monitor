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
output_pin = 13
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
vac_chan = AnalogIn(mcp, MCP.P0)
start_chan = AnalogIn(mcp, MCP.P1)
manual_start_chan = AnalogIn(mcp, MCP.P2)
end_chan = AnalogIn(mcp, MCP.P3)

last_read0 = 0       # this keeps track of the last potentiometer value
last_read1 = 0       # this keeps track of the last potentiometer value
last_read2 = 0       # this keeps track of the last potentiometer value
tolerance = 250     # to keep from being jittery we'll only change
                   # volume when the pot has moved a significant amount
                   # on a 16-bit ADC


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
   # we'll assume that the pot didn't move
   trim_pot_changed = False

   # read the analog pin
   vacuum_raw = vac_chan.value
   start_raw = start_chan.value
   manual_start_raw = manual_start_chan.value
   end_raw = end_chan.value

   # how much has it changed since the last read?
   vac_change = abs(vacuum_raw - last_vac) > tolerance
   start_change = abs(start_raw - last_start) > tolerance
   manual_start_change = abs(manual_start_raw - last_manual_start) > tolerance
   end_change = abs(end_raw - last_end) > tolerance

   if vac_change or start_change or manual_start_change or end_change:
       trim_pot_changed = True
   if trim_pot_changed == True
   # if True:
       # convert 16bit adc0 (0-65535) trim pot read into 0-5volt level
       adc_input_voltage = 5
       voltage0 = remap_range(vacuum_raw, 0, 65535, 0, adc_input_voltage)
       voltage0a = np.interp(vacuum_raw,adc_value_range,adc_voltage_range)
       pressure0 = remap_range(trim_pot0, 0, 65535, -14.5, 30)
       pressureinhg0 = remap_range(trim_pot0, 0, 65535, -29.52, 60)

       start_voltage = np.interp(start_raw,adc_value_range,adc_voltage_range)
       manual_start_voltage = np.interp(manual_start_raw,adc_value_range,adc_voltage_range)
       end_voltage = np.interp(end_raw,adc_value_range,adc_voltage_range)

       # print voltage
       print('Current State = {}raw, {:0.3f}v, {:0.3f}v, {:0.3f}psi, {:0.3f}inHg'.format(trim_pot0,voltage0,voltage0a,pressure0,pressureinhg0))
       print('start: {:0.3f}v, manual start: {:0.3f}v, end: {:0.3f}v'.format(start_voltage,manual_start_voltage,end_voltage))

       # save the potentiometer reading for the next loop
       last_vac = vacuum_raw
       last_start = start_raw
       last_manual_start = manual_start_raw
       last_end = end_raw

   # hang out and do nothing for a half second
   time.sleep(0.05)