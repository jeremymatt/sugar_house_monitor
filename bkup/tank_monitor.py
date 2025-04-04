import serial
import time
import RPi.GPIO as GPIO
import numpy as np
import adafruit_character_lcd.character_lcd_rgb_i2c as character_lcd
import board
import busio
import tank_vol_fcns as TVF

lcd_red = [100,0,0]
lcd_off = [0,0,0]

#Set the GPIO pin numbering mode
GPIO.setmode(GPIO.BCM)

# Set up the serial port for UART communication
brookside_uart = serial.Serial("/dev/serial0", baudrate=9600, timeout=0.5)
roadside_uart = serial.Serial("/dev/ttyAMA5", baudrate=9600, timeout=0.5)


def init_display():        
    lcd_columns = 16
    lcd_rows = 2
    i2c = busio.I2C(board.SCL, board.SDA)
    lcd = character_lcd.Character_LCD_RGB_I2C(i2c, lcd_columns, lcd_rows)
    lcd.color = lcd_red
    return lcd

# def calculate_checksum(buffer):
#     # Ensures that the checksum is constrained to a single byte (8 bits)
#     return (buffer[0] + buffer[1] + buffer[2]) & 0xFF

# def read_distance(uart):
#     uart.write(bytes([COM]))
#     time.sleep(0.1)
#     Distance = None
#     if uart.in_waiting > 0:
#         time.sleep(0.004)
#         if uart.read(1) == b'\xff':  # Judge packet header
#             buffer_RTT = uart.read(3)
#             if len(buffer_RTT) == 3:
#                 CS = calculate_checksum(b'\xff' + buffer_RTT)
#                 if buffer_RTT[2] == CS:
#                     Distance = (buffer_RTT[0] << 8) + buffer_RTT[1]  # Calculate distance

#     return Distance
    
def exit_program(lcd):
        print("\nDone.\nExiting.")
        lcd.clear()
        lcd.message = 'BYE'
        time.sleep(5)
        lcd.clear()
        lcd.color = lcd_off
        brookside_uart.close()
        roadside_uart.close()

num_to_average = 8
delay = 0.25
"""
brookside = TVF.TANK('brookside',brookside_uart,num_to_average,delay)
brookside.get_tank_rate(60)
print('rate: {} time: {}'.format(brookside.tank_rate,brookside.remaining_time))

"""

def main():
    brookside = TVF.TANK('brookside',brookside_uart,num_to_average,delay)
    roadside = TVF.TANK('roadside',roadside_uart,num_to_average,delay)
    lcd = init_display()
    lcd.clear()
    run = True
    measure = True
    prev_msg = ''
    try:
        while run:
            if measure:
                brookside.update_status()
                roadside.update_status()
                mins_back = 5
                brookside.get_tank_rate(mins_back)
                roadside.get_tank_rate(mins_back)

                cur_msg = 'BS:{}/{}\nRS:{}/{}'.format(np.round(brookside.current_gallons,0),brookside.tank_rate,np.round(roadside.current_gallons,0),roadside.tank_rate)

                if not cur_msg == prev_msg:
                    lcd.clear()
                    lcd.message = cur_msg
                    prev_msg = cur_msg

            if lcd.down_button:
                measure = False
                lcd.clear()
                prev_msg = 'PAUSED'
                lcd.message = prev_msg
            if lcd.up_button:
                measure = True
            if lcd.select_button:
                run = False
                exit_program(lcd)
            else:
                time.sleep(13)  # Adjust the delay as needed

        
    except(KeyboardInterrupt, SystemExit): #when you press ctrl+c
        exit_program(lcd)

if __name__ == "__main__":
    main()

