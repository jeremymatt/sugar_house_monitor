import serial
import time
import RPi.GPIO as GPIO
import numpy as np


# Set up the serial port for UART communication
uart1 = serial.Serial("/dev/serial0", baudrate=9600, timeout=0.5)
# uart = serial.Serial("/dev/ttyS0", baudrate=9600, timeout=0.5)
# uart = serial.Serial("/dev/ttyS1", baudrate=9600, timeout=0.5)
uart2 = serial.Serial("/dev/ttyAMA5", baudrate=9600, timeout=0.5)

# Constants
COM = 0x55

def calculate_checksum(buffer):
    # Ensures that the checksum is constrained to a single byte (8 bits)
    return (buffer[0] + buffer[1] + buffer[2]) & 0xFF

def read_distance(uart):
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
            distance1 = read_distance(uart1)
            if not isinstance(distance1,type(None)):
                distance1 /= 25.4
                distance1 = np.round(distance1,2)
            else:
                distance1 = 'n/a'

                
            distance2 = read_distance(uart2)
            if not isinstance(distance2,type(None)):
                distance2 /= 25.4
                distance2 = np.round(distance2,2)
            else:
                distance2 = 'n/a'

            print('Distances:\n   Brookside: {}in\n   Roadside: {}in\n'.format(distance1,distance2))

            time.sleep(0.25)  # Adjust the delay as needed

        
    except(KeyboardInterrupt, SystemExit): #when you press ctrl+c
        print("Done.\nExiting.")
        uart1.close()
        uart2.close()

if __name__ == "__main__":
    main()


