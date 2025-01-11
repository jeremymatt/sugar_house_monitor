import serial
import time
import RPi.GPIO as GPIO
import numpy as np


#Set the GPIO pin numbering mode
GPIO.setmode(GPIO.BCM)

# Set up the serial port for UART communication
uart = serial.Serial("/dev/serial0", baudrate=9600, timeout=0.5)

# Constants
COM = 0x55

def calculate_checksum(buffer):
    # Ensures that the checksum is constrained to a single byte (8 bits)
    return (buffer[0] + buffer[1] + buffer[2]) & 0xFF

def read_distance():
    uart.write(bytes([COM]))
    time.sleep(0.1)
    Distance = None
    if uart.in_waiting > 0:
        time.sleep(0.004)
        if uart.read(1) == b'\xff':  # Judge packet header
            buffer_RTT = uart.read(3)
            if len(buffer_RTT) == 3:
                CS = calculate_checksum(b'\xff' + buffer_RTT)
                if buffer_RTT[2] == CS:
                    Distance = (buffer_RTT[0] << 8) + buffer_RTT[1]  # Calculate distance

    return Distance
    

def main():
    try:
        while True:
            # print('Reading distance...')
            distance = read_distance()
            if not isinstance(distance,type(None)):
                distance /= 25.4
                distance = np.round(distance,2)
                print(f"Distance: {distance}in")

            time.sleep(0.25)  # Adjust the delay as needed

        
    except(KeyboardInterrupt, SystemExit): #when you press ctrl+c
        print("Done.\nExiting.")
        GPIO.output(fire_pin,GPIO.LOW)
        uart.close()

if __name__ == "__main__":
    main()



"""

import serial
import time
import math
import numpy as np
import RPi.GPIO as GPIO

#Threshold speed in feet per second
threshold_speed = 2.5
#number of readings below threshold speed before detonation
slow_reading_threshold = 3 
#Number of instantaneous speed readings to average
num_to_average = 5
#Pin number for the armed LED (indicates that average speed has exceeded the threshold speed)
arm_pin = 13
#Pin number to trigger the detonation
fire_pin = 9

GPIO.setmode(GPIO.BCM)
#set pin 13 as the output
GPIO.setup(arm_pin,GPIO.OUT)
GPIO.output(arm_pin,GPIO.LOW)
GPIO.setup(fire_pin,GPIO.OUT)
GPIO.output(fire_pin,GPIO.LOW)


with open("/home/circuit/error_log.txt",'a') as error_log:
    error_log.write('set up output pins\n')

port = "/dev/serial0"
sp = serial.Serial(port, baudrate = 9600, timeout = 0.5)
meas_dist = sp.write(serial.to_bytes(0x55))

def get_msg(timeout = 2):
    msg = b''
    now = time.time()
    serialPort.write(serial.to_bytes(0x55))
    time.sleep(0.2)
    while msg == b'':
        if time.time()-now>timeout:
            print('message not received in {}s'.format(timeout))
            return None
        try:
            string = serialPort.readline()
            msg = string
        except:
            msg = b''
    return msg
"""
    
def get_msg():
    msg = None
    while isinstance(msg,type(None)):
        string = serialPort.readline()
        string = string.decode().strip()
        msg = parseGPS(string)
    return msg
"""


def blink_arm(num_blinks = 5,blink_len = 0.25):
    for i in range(num_blinks):
        GPIO.output(arm_pin,GPIO.LOW)
        time.sleep(blink_len)
        GPIO.output(arm_pin,GPIO.HIGH)
        time.sleep(blink_len)


speed_list = []
for i in range(num_to_average):
    speed_list.append(np.nan)

#Init variables as placeholders
start_lat_lon = None
msg = None
armed = False
armed_ctr = 0
mean_speed = -999

#Blink the armed LED as an indicator that the device is active
blink_arm(20,0.1)
GPIO.output(arm_pin,GPIO.LOW)


with open("/home/circuit/error_log.txt",'a') as error_log:
    error_log.write('variable placehold init finished\n')

port = "/dev/serial0"
serialPort = serial.Serial(port, baudrate = 9600, timeout = 0.5)

with open("/home/circuit/error_log.txt",'a') as error_log:
    error_log.write('Opened serial port\n')
try:
    ctr = 0
    while True:
        msg = get_msg()
        if not isinstance(msg,type(None)):
            print('msg: {}'.format(msg))

        time.sleep(.5)

        


except(KeyboardInterrupt, SystemExit): #when you press ctrl+c
    print("Done.\nExiting.")
    serialPort.close()
"""