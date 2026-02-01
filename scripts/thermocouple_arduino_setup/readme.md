# Thermocouple Arduino System Documentation

## 1. Overview

The Thermocouple Arduino system monitors evaporator stack temperature using a K-type thermocouple and MCP9600 amplifier. The system provides:

- **Real-time temperature monitoring**: Stack (hot junction) and ambient (cold junction) temperatures
- **Local LCD display**: 20x4 character display showing current readings and rolling window averages
- **WiFi data upload**: Automatic upload to WordPress API endpoint (`/api/ingest_stacktemp.php`)
- **Rolling window averaging**: Computes 15-minute, 30-minute, 45-minute, and 60-minute averages
- **Fault tolerance**: Automatic sensor recovery, WiFi reconnection, and stale data detection

**Hardware Platform**: Arduino with WiFiS3 support (Arduino Uno R4 WiFi recommended)

**Key Design Features**:
- I2C bus recovery for sensor faults
- Automatic WiFi reconnection with exponential backoff
- Stale reading detection with visual warning
- Network status indicator on LCD
- Per-minute sample buckets for efficient averaging

## 2. Setup Instructions

### Prerequisites

**Required Hardware**:
- Arduino with WiFiS3 (Arduino Uno R4 WiFi or similar)
- MCP9600 thermocouple amplifier breakout board
- K-type thermocouple probe
- 20x4 I2C LCD display (HD44780 compatible)
- USB cable for programming
- 5V power supply (1A minimum)

**Required Arduino Libraries** (install via Arduino Library Manager):
- `Adafruit_MCP9600` by Adafruit
- `hd44780` by Bill Perry (I2C LCD driver)
- `WiFiS3` (built-in for Uno R4 WiFi)
- `ArduinoHttpClient` by Arduino

### Hardware Assembly

1. **Connect MCP9600 thermocouple amplifier** (see [Hardware Overview](#4-hardware-overview-wiring) for full wiring)
2. **Connect I2C LCD display** to same I2C bus
3. **Attach K-type thermocouple** to MCP9600 screw terminals
4. **Power Arduino** via USB or barrel jack (5V)

### Software Configuration

1. **Open sketch** in Arduino IDE:
   ```
   sugar_house_monitor/scripts/thermocouple_arduino.ino
   ```

2. **Configure WiFi and API settings** (edit lines 14-51, or use compiler flags):
   ```cpp
   #define SHM_WIFI_SSID "YOUR_WIFI_SSID"
   #define SHM_WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
   #define SHM_API_KEY "YOUR_API_KEY"  // From gen_credentials.py
   #define SHM_API_HOST "mattsmaplesyrup.com"
   #define SHM_API_PATH "/sugar_house_monitor/api/ingest_stacktemp.php"
   ```

3. **Optional: Adjust timing and sampling settings**:
   ```cpp
   #define SHM_SAMPLE_INTERVAL_MS 1000UL      // 1 second sampling
   #define SHM_WINDOW_SIZE_MINUTES 15         // Window size for averaging
   #define SHM_USE_TLS 1                      // HTTPS (set 0 for HTTP)
   #define SHM_I2C_CLOCK_HZ 50000UL           // I2C bus speed
   ```

4. **Compile and upload**:
   - Select Board: **Arduino Uno R4 WiFi**
   - Select Port: (your Arduino's COM port)
   - Click **Upload**

5. **Monitor serial output** (115200 baud):
   ```
   Tools > Serial Monitor
   ```
   Should see: "MCP9600 HW test", "Found MCP9600!", "WiFi connected", "Upload ok"

### Alternative: Compiler Flags

For secure deployments (avoid hardcoding secrets), use compiler flags in `platform.local.txt` or build script:
```
build.extra_flags=
  -DSHM_WIFI_SSID=\"YourSSID\"
  -DSHM_WIFI_PASSWORD=\"YourPassword\"
  -DSHM_API_KEY=\"YourAPIKey\"
```

## 3. Controls Summary

### Operating Modes

The Arduino runs autonomously once powered. No manual controls required during normal operation.

**Normal Operation**:
- Samples temperature every 1 second (configurable)
- Updates LCD display with current readings and averages
- Uploads to server when WiFi connected
- Network status indicator: "NET↑" (upload OK) or "NET↓" (upload failed/disconnected)

**Stale Reading Detection**:
- If sensor fails to produce valid readings for >2 seconds, LCD alternates between:
  - Current readings
  - "WARNING - READING STALE - Last Update: MM:SS ago"
- Automatic sensor recovery via I2C bus reset

**Serial Monitor Commands** (for debugging):
- No interactive commands; serial output is log-only
- Baud rate: 115200

### Key Configuration Constants

| Constant | Default | Purpose |
|----------|---------|---------|
| `SHM_SAMPLE_INTERVAL_MS` | 1000 | Temperature sampling interval (milliseconds) |
| `SHM_WINDOW_SIZE_MINUTES` | 15 | Rolling average window size (minutes) |
| `SHM_USE_TLS` | 1 | Use HTTPS (1) or HTTP (0) for uploads |
| `SHM_I2C_CLOCK_HZ` | 50000 | I2C bus clock speed (50 kHz default for reliability) |
| `SHM_UPLOAD_RETRY_MS` | 30000 | Retry interval after upload failure (milliseconds) |
| `SHM_WIFI_RETRY_MS` | 500 | Delay between WiFi connection attempts (milliseconds) |
| `SHM_WIFI_MAX_ATTEMPTS` | 10 | Max WiFi attempts before timeout |
| `SENSOR_FAILURE_THRESHOLD` | 5 | Consecutive read failures before recovery attempt |
| `HTTP_FAILURE_THRESHOLD` | 3 | Consecutive HTTP failures before WiFi reset |

**Tuning Notes**:
- Lower `SHM_I2C_CLOCK_HZ` (e.g., 50 kHz) improves reliability on long cables or noisy environments
- Increase `SHM_SAMPLE_INTERVAL_MS` to reduce server load (e.g., 5000 for 5-second sampling)
- Set `SHM_USE_TLS=0` for HTTP-only servers (not recommended for production)

### LCD Display Format

**Normal Mode**:
```
Ambient: 0075F    NET↑
Stack: 0425F
015:0420F||030:0418F
045:0415F||060:0412F
```
- Row 1: Ambient temperature, network status
- Row 2: Current stack temperature
- Row 3: 15-min and 30-min averages
- Row 4: 45-min and 60-min averages

**Warning Mode** (stale reading):
```
      WARNING
   READING STALE
   Last Update:
  00:15 (mm:ss) ago
```

### Data Upload Behavior

- **Upload trigger**: Every successful temperature reading (1 Hz with default settings)
- **Upload format**: JSON POST to `ingest_stacktemp.php`:
  ```json
  {
    "api_key": "YOUR_API_KEY",
    "readings": [{
      "stack_temp_f": 425.0,
      "ambient_temp_f": 75.0,
      "source_timestamp": "2026-01-31T12:34:56Z"
    }]
  }
  ```
- **Retry logic**: If upload fails, retry after `SHM_UPLOAD_RETRY_MS` (30 seconds)
- **WiFi recovery**: After 3 consecutive HTTP failures, reset WiFi stack

## 4. Hardware Overview & Wiring

### Component List

| Component | Part Number / Spec | Purpose |
|-----------|-------------------|---------|
| **Arduino** | Uno R4 WiFi | Main controller with WiFi |
| **Thermocouple Amplifier** | MCP9600 (Adafruit #4101) | K-type thermocouple cold junction compensation |
| **Thermocouple** | K-type, high-temp (up to 500°C+) | Evaporator stack temperature probe |
| **LCD Display** | 20x4 I2C HD44780 | Local temperature display |
| **Power Supply** | 5V 1A USB or barrel jack | Arduino power |

### I2C Addressing

- **MCP9600**: 0x67 (hardcoded in sketch, configurable via hardware address pins)
- **LCD**: Auto-detected by hd44780 library (typically 0x27 or 0x3F)

### Wiring Diagram

**I2C Bus Connections**:
```
Arduino Uno R4 WiFi
  A4 (SDA) ──┬─── MCP9600 SDA
             └─── LCD SDA

  A5 (SCL) ──┬─── MCP9600 SCL
             └─── LCD SCL

  5V       ──┬─── MCP9600 VIN (or 3.3V depending on breakout)
             └─── LCD VCC

  GND      ──┬─── MCP9600 GND
             └─── LCD GND
```

**MCP9600 Thermocouple Connections**:
```
MCP9600 Breakout Board
  T+ ────── K-type thermocouple red wire (+)
  T- ────── K-type thermocouple yellow wire (-)
```

**Notes**:
- Pull-up resistors (4.7kΩ) on SDA/SCL usually included on I2C modules
- I2C bus length: Keep wires <1 meter for reliability (use shielded cable for longer runs)
- MCP9600 can run on 3.3V or 5V depending on breakout board version

### Thermocouple Installation

**Physical Placement**:
- Mount K-type thermocouple probe in evaporator stack
- Ensure probe tip exposed to stack gases (not touching metal directly)
- Use high-temperature silicone sealant or compression fitting for mounting
- Route thermocouple wire away from AC power lines (EMI prevention)

**Thermocouple Specifications**:
- Type: K (Chromel-Alumel)
- Temperature range: Typically -200°C to +1260°C (-330°F to +2300°F)
- For maple syrup stack monitoring: Expect 100-250°C (212-482°F) during operation

### LCD Contrast Adjustment

If LCD text not visible:
- Most I2C LCD modules have onboard potentiometer for contrast
- Turn potentiometer with small screwdriver until text visible
- Typical setting: mid-range rotation

### Power Requirements

- **Arduino idle**: ~100 mA
- **Arduino + WiFi active**: ~250 mA peak
- **Total system**: ~300 mA @ 5V
- **Recommended supply**: 5V 1A (USB charger or barrel jack)

## 5. Additional Details

### MCP9600 Configuration

The sketch configures the MCP9600 with optimal settings for K-type thermocouple:

- **Thermocouple type**: K (set in `initMcp()`)
- **ADC resolution**: 18-bit (0.015625°C resolution)
- **Ambient resolution**: 0.0625°C
- **Filter coefficient**: 3 (light filtering, fast response)
- **Alert 1**: Configured for 30°C rising temperature (not used in current implementation)

**Cold Junction Compensation**:
- MCP9600 automatically compensates for ambient temperature at the sensor board
- Ambient reading displayed on LCD for verification

### Rolling Window Averaging

**Implementation**:
- Samples stored in per-minute buckets (60 samples per bucket at 1 Hz)
- Four configurable windows: 1×, 2×, 3×, 4× `SHM_WINDOW_SIZE_MINUTES` (default: 15, 30, 45, 60 min)
- Buffer size: 4× window size to support longest window
- Average computed across all samples in window

**Memory usage**:
- 4 bytes per minute bucket sum (float)
- 2 bytes per minute sample count (uint16_t)
- Total: 6 bytes × 60 minutes = 360 bytes for default configuration

### WiFi Connection Management

**Connection behavior**:
- On boot: Attempt WiFi connection immediately
- Timeout: 5 seconds (10 attempts × 500ms)
- On disconnect: Automatic reconnection attempts every 500ms
- Network time sync: Automatic via `WiFi.getTime()` for ISO timestamps

**TLS/HTTPS**:
- Enabled by default (`SHM_USE_TLS=1`)
- Arduino Uno R4 WiFi handles certificate validation automatically
- Timeout: 15 seconds per HTTP request
- If TLS handshake fails repeatedly, WiFi stack reset attempted

### I2C Bus Recovery

**Problem**: I2C devices can lock up (SDA stuck LOW) due to power glitches or EMI

**Solution**: `recoverI2cBus()` function:
1. Set SCL/SDA to INPUT_PULLUP, check bus state
2. If stuck, toggle SCL up to 16 times (clock out stuck bit)
3. Generate I2C STOP condition (reset bus protocol state)
4. Reinitialize MCP9600 and LCD after recovery

**Triggered by**:
- 5 consecutive sensor read failures
- 30-second cooldown between recovery attempts

### Sensor Fault Handling

**Failure detection**:
- NaN (Not a Number) returned from `mcp.readThermocouple()` or `mcp.readAmbient()`
- Increments `sensorFailureCount` on each NaN reading

**Recovery sequence**:
1. After 5 consecutive failures: Attempt I2C bus recovery
2. Reinitialize MCP9600 (up to 3 retry attempts)
3. If successful: Reset failure count, resume sampling
4. If failed: Continue sampling, retry recovery after 30 seconds

**User feedback**:
- Serial log: "Sensor read failed; skipping sample"
- LCD: Stale warning appears if failures persist >2 seconds

### HTTP Upload Retry Logic

**Upload states**:
- **Success**: HTTP 200-299 response → Reset failure count, schedule next upload immediately
- **Failure**: HTTP error or negative status → Increment failure count, retry after 30 seconds
- **WiFi reset trigger**: 3 consecutive failures → Full WiFi disconnect/reconnect

**Network status indicator**:
- "NET↑" (arrow up): Last upload successful, WiFi connected
- "NET↓" (arrow down): Upload failed or WiFi disconnected

### Serial Logging

**Log output** (115200 baud):
```
MCP9600 HW test
Found MCP9600!
Ambient Resolution set to: 0.0625°C
ADC resolution set to 18 bits
Thermocouple type set to K type
Filter coefficient value set to: 3
Alert #1 temperature set to 30
LCD initialized
Connecting to WiFi SSID YourSSID
WiFi connected, IP: 192.168.1.42
------------------------------
Stack (hot junction): 425.23 F
Ambient (cold junction): 75.12 F
ADC: 5400 uV
POST mattsmaplesyrup.com:443/sugar_house_monitor/api/ingest_stacktemp.php
Upload ok (HTTP 200)
```

**Key log messages**:
- `Sensor not found`: MCP9600 not detected on I2C bus (check wiring)
- `Upload failed (HTTP -1)`: TLS/connection issue
- `WiFi disconnected`: Lost connection, will auto-reconnect
- `Reinitializing MCP9600...`: Sensor recovery attempt
- `I2C recovery start`: Bus stuck, attempting reset

## 6. Error Information & Troubleshooting

### Common Errors

**"Sensor not found. Check wiring!"** (boot failure, infinite loop):
- **Cause**: MCP9600 not responding on I2C bus at address 0x67
- **Solutions**:
  1. Check I2C wiring: SDA (A4), SCL (A5), VIN (5V or 3.3V), GND
  2. Verify MCP9600 power LED lit (if present on breakout)
  3. Run I2C scanner sketch to detect device address
  4. Check I2C address jumpers on MCP9600 breakout (should be 0x67)
  5. Try different I2C clock speed: Set `SHM_I2C_CLOCK_HZ 100000UL` (100 kHz)

**"LCD not found or init failed (status=X)"**:
- **Cause**: I2C LCD not detected
- **Solutions**:
  1. Check LCD I2C wiring and power
  2. Adjust LCD contrast potentiometer
  3. Run I2C scanner to find LCD address (typically 0x27 or 0x3F)
  4. If LCD at different address, modify hd44780 library or use different library
- **Note**: Sketch continues without LCD; only affects local display

**"WiFi connect attempt timed out"**:
- **Cause**: Cannot connect to WiFi network
- **Solutions**:
  1. Verify SSID and password correct (case-sensitive)
  2. Check WiFi network is 2.4 GHz (Arduino Uno R4 WiFi doesn't support 5 GHz)
  3. Move Arduino closer to WiFi access point
  4. Check access point not using MAC filtering
  5. Try open network first (no password) to isolate auth issues

**"Upload failed (HTTP -1)"** (repeated):
- **Cause**: TLS/HTTPS connection failure or network issue
- **Solutions**:
  1. Check API_HOST and API_PATH correct
  2. Verify server TLS certificate valid
  3. Try HTTP instead: Set `SHM_USE_TLS 0` (temporary test only)
  4. Check firewall not blocking outbound HTTPS
  5. Monitor serial for "WiFi disconnected" → network stability issue

**"Upload failed (HTTP 401)" or "HTTP 403"**:
- **Cause**: API authentication failure
- **Solutions**:
  1. Verify `SHM_API_KEY` matches server config
  2. Check API endpoint path correct: `/sugar_house_monitor/api/ingest_stacktemp.php`
  3. Test API endpoint with curl:
     ```bash
     curl -X POST https://mattsmaplesyrup.com/sugar_house_monitor/api/ingest_stacktemp.php \
       -H "Content-Type: application/json" \
       -H "X-API-Key: YOUR_API_KEY" \
       -d '{"api_key":"YOUR_API_KEY","readings":[{"stack_temp_f":100,"ambient_temp_f":70}]}'
     ```

**NaN readings (LCD shows 0000F, serial shows "NaN")**:
- **Cause**: Thermocouple disconnected or MCP9600 fault
- **Solutions**:
  1. Check thermocouple connected to T+ and T- screw terminals
  2. Verify thermocouple not shorted or open circuit (test with multimeter)
  3. Check for EMI (move thermocouple wire away from AC power)
  4. Wait for automatic I2C bus recovery (triggered after 5 consecutive failures)
  5. Manually reset Arduino (power cycle)

**Stale warning on LCD (alternating display)**:
- **Cause**: Sensor failing to produce valid readings for >2 seconds
- **Solutions**:
  1. Check serial monitor for specific error (sensor read failure, I2C issue)
  2. Wait for automatic recovery (30-second cooldown)
  3. Power cycle Arduino if recovery fails
  4. Check I2C wiring for loose connections

**LCD backlight on but no text visible**:
- **Cause**: Contrast setting incorrect
- **Solution**: Adjust contrast potentiometer on LCD backpack (small blue or white component)

**LCD text garbled or random characters**:
- **Cause**: I2C communication error or incorrect LCD voltage
- **Solutions**:
  1. Check power supply stable 5V
  2. Verify I2C pull-up resistors present
  3. Lower I2C clock speed: `SHM_I2C_CLOCK_HZ 10000UL` (10 kHz)
  4. Shorter I2C wires (<30 cm recommended)

### Diagnostic Procedures

**Test I2C bus**:
1. Upload I2C scanner sketch (Arduino Examples > Wire > i2c_scanner)
2. Open serial monitor (115200 baud)
3. Should detect MCP9600 at 0x67 and LCD at 0x27 or 0x3F
4. If devices not found: Wiring issue

**Test thermocouple**:
1. Disconnect thermocouple from MCP9600
2. Measure resistance across T+ and T- terminals: Should read ~0Ω (low resistance)
3. If open circuit (infinite resistance): Thermocouple broken
4. Heat thermocouple tip with lighter, measure voltage: Should see microvolts change
5. K-type thermocouple: ~41 µV/°C

**Test WiFi connectivity**:
1. Monitor serial output during boot
2. Should see: "Connecting to WiFi SSID", then "WiFi connected, IP: X.X.X.X"
3. If "WiFi connect attempt timed out": Network issue (see WiFi troubleshooting above)

**Test API endpoint manually**:
```bash
# HTTP test (if TLS disabled)
curl -v -X POST http://mattsmaplesyrup.com/sugar_house_monitor/api/ingest_stacktemp.php \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"api_key":"YOUR_API_KEY","readings":[{"stack_temp_f":100,"ambient_temp_f":70}]}'

# Should return HTTP 200 with JSON response
```

### Performance Optimization

**Reduce upload frequency** (lower server load):
```cpp
#define SHM_SAMPLE_INTERVAL_MS 5000UL  // Sample every 5 seconds instead of 1
```

**Improve I2C reliability** (noisy environment):
```cpp
#define SHM_I2C_CLOCK_HZ 10000UL  // Lower clock speed from 50 kHz to 10 kHz
```

**Disable TLS** (HTTP-only server, not recommended for production):
```cpp
#define SHM_USE_TLS 0  // Use HTTP on port 80 instead of HTTPS
```

### Factory Reset / Reconfiguration

1. Edit sketch with new WiFi/API credentials
2. Re-upload to Arduino (overwrites previous config)
3. All settings stored in flash memory, not EEPROM (no persistent storage)

### Getting Help

**Collect diagnostic info**:
1. Open serial monitor (115200 baud)
2. Capture boot sequence and error messages
3. Note LED blink patterns on Arduino
4. Check LCD display for error messages
5. Measure voltages: 5V rail, I2C SDA/SCL

**Schematic reference**:
See `schematics/temp_arduino/` for KiCad schematic files (if available)

**Report issues**:
https://github.com/jeremymatt/sugar_house_monitor/issues

Include:
- Arduino board model (Uno R4 WiFi, etc.)
- MCP9600 breakout vendor (Adafruit, generic, etc.)
- Serial monitor output (sanitize API_KEY!)
- Photo of wiring setup
- Error messages from LCD
