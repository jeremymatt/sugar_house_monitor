# Thermocouple Arduino setup (Uno R4)

## Overview
The Arduino Uno R4 reads a MCP9600 thermocouple board over I2C, renders temperatures and windowed averages on an I2C LCD, and uploads stack/ambient readings to the Sugar House Monitor API.

## Setup
### Add Uno R4 board support
1) Open Arduino IDE.
2) Tools -> Board -> Boards Manager.
3) Install the Arduino UNO R4 Boards package (includes WiFiS3).
4) Select the board that matches your hardware (Arduino Uno R4 WiFi or Uno R4 Minima).

### Install libraries (Arduino IDE -> Library Manager)
- Adafruit MCP9600
- Adafruit BusIO
- hd44780 (by Bill Perry)
- ArduinoHttpClient

### Configure the firmware (scripts/thermocouple_arduino.ino)
Update the constants near the top of the file or pass them as compiler defines:
- SHM_WIFI_SSID and SHM_WIFI_PASSWORD
- SHM_API_KEY
- SHM_API_HOST and SHM_API_PATH (default posts to /sugar_house_monitor/api/ingest_stacktemp.php)
- SHM_USE_TLS (1 for HTTPS, 0 for HTTP)
- SHM_SAMPLE_INTERVAL_MS
- SHM_WINDOW_SIZE_MINUTES
- I2C_ADDRESS (default 0x67 for the MCP9600 board)

Open network vs password-protected network:
- For open WiFi, set SHM_WIFI_PASSWORD to an empty string (""), so the sketch calls WiFi.begin(SSID) without a password.
- For password-protected WiFi, set SHM_WIFI_PASSWORD to the network password.

### Upload the sketch
1) Connect the Uno R4 via USB.
2) Select the correct board + port in the Arduino IDE.
3) Upload scripts/thermocouple_arduino.ino.
4) Open Serial Monitor at 115200 baud to verify startup logs.

## Controls summary
- No physical buttons; use the Serial Monitor for logs.
- LCD rows show ambient and stack temperatures plus rolling window averages.

## Hardware overview
- Arduino Uno R4.
- MCP9600 I2C thermocouple board (I2C address 0x67 by default).
- I2C LCD (hd44780 backpack).
- K-type thermocouple connected to the MCP9600 board.

Wiring diagram:
```
Arduino Uno R4          MCP9600 / LCD
---------------------------------------------
5V (or 3.3V per board) -> MCP9600 VIN
GND -------------------> MCP9600 GND, LCD GND
SDA -------------------> MCP9600 SDA, LCD SDA
SCL -------------------> MCP9600 SCL, LCD SCL
Thermocouple + / - ----> MCP9600 TC+ / TC-
```

## Additional details
- The sketch maintains rolling averages in 1x, 2x, 3x, and 4x window_size_minutes buckets for LCD display.
- Uploads include an API key in both the X-API-Key header and the api_key query param.
- The I2C clock is set by SHM_I2C_CLOCK_HZ (default 50 kHz) and can be adjusted for long cable runs.

## Error info
- Sensor read failures increment a counter; the sketch attempts I2C recovery and MCP9600 reinit after repeated failures.
- WiFi/HTTP failures trigger retries; repeated HTTP failures can reset the WiFi client.
- If readings go stale, the LCD toggles a warning screen until fresh data returns.
