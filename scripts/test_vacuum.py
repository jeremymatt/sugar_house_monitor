import os
import time
import busio
import digitalio
import board
import adafruit_mcp3xxx.mcp3008 as MCP
from adafruit_mcp3xxx.analog_in import AnalogIn
import RPi.GPIO as GPIO

GPIO.setmode(GPIO.BCM)

#set pin 13 as the output
output_pin = 13
GPIO.setup(output_pin,GPIO.OUT)
GPIO.output(output_pin,GPIO.LOW)

# create the spi bus
spi = busio.SPI(clock=board.SCK, MISO=board.MISO, MOSI=board.MOSI)

# create the cs (chip select)
cs = digitalio.DigitalInOut(board.D5)

# create the mcp object
mcp = MCP.MCP3008(spi, cs)

# create an analog input channel on pin 0
chan0 = AnalogIn(mcp, MCP.P0)
chan1 = AnalogIn(mcp, MCP.P1)
chan2 = AnalogIn(mcp, MCP.P2)

print('Raw ADC Value: ', chan0.value)
print('ADC Voltage: ' + str(chan0.voltage) + 'V')

print('Raw ADC Value: ', chan1.value)
print('ADC Voltage: ' + str(chan1.voltage) + 'V')

print('Raw ADC Value: ', chan2.value)
print('ADC Voltage: ' + str(chan2.voltage) + 'V')

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
   trim_pot0 = chan0.value

   # how much has it changed since the last read?
   pot_adjust0 = abs(trim_pot0 - last_read0)

   if (pot_adjust0 > tolerance):
       trim_pot_changed = True
   # trim_pot_changed = True
   if True:
       # convert 16bit adc0 (0-65535) trim pot read into 0-5volt level
       adc_input_voltage = 5
       voltage0 = remap_range(trim_pot0, 0, 65535, 0, adc_input_voltage)
       pressure0 = remap_range(trim_pot0, 0, 65535, -14.5, 30)

    #    if voltage0<0.9:
    #        GPIO.output(output_pin,GPIO.HIGH)
    #        print("BOOM")
    #    else:
    #        GPIO.output(output_pin,GPIO.LOW)


       # print voltage
       print('Current State = {:0.3f}v, {:0.3f}psi'.format(voltage0,pressure0))

       # save the potentiometer reading for the next loop
       last_read0 = trim_pot0

   # hang out and do nothing for a half second
   time.sleep(0.05)