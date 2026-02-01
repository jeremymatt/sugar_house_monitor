# Sugar House Monitor

Live monitoring and control system for maple syrup production, tracking tank levels, transfer pump operation, evaporator flow, and vacuum across multiple Raspberry Pi devices with a WordPress-hosted web interface.

## System Overview

The Sugar House Monitor is a distributed monitoring system consisting of:

- **Tank Pi**: Monitors Brookside and Roadside tank levels using ultrasonic sensors; computes volume, flow rate, and fill estimates
- **Pump Pi**: Controls and monitors the transfer pump with safety interlocks; includes LED status indicator
- **Display Pi**: Provides real-time fullscreen display of evaporator flow and system status
- **Web Server**: WordPress-hosted API endpoints and web UI for remote monitoring and data visualization

## Architecture

### Data Flow

1. **Field Devices** (Tank Pi, Pump Pi) collect sensor data and write to local SQLite databases
2. **Upload Services** batch and POST readings/events to WordPress API endpoints (`ingest_tank.php`, `ingest_pump.php`, `ingest_vacuum.php`)
3. **Server Processing** validates data, updates server databases, and triggers `process_status.py` to generate status JSON files
4. **Web Interface** (`web/index.html`) and Display Pi read status JSON and history APIs for visualization
5. **CSV Exports** available via `export_csv.php` and `/downloads.html`

### Communication

- **Uplink**: Pi devices → WordPress API (HTTPS with shared API key authentication)
- **Downlink**: Web UI / Display Pi → WordPress (HTTP/HTTPS, reads status JSON and history APIs)
- **Local IPC**: Microservices on each Pi communicate via shared memory cache (`/dev/shm/*.json`)

## Subsystem Documentation

### Field Devices

- **[Pump Pi Setup](scripts/pump_pi_setup/readme.md)** - Transfer pump controller, LED status indicator, ADC service, vacuum monitoring, and systemd service configuration
- **[Thermocouple Arduino Setup](scripts/thermocouple_arduino_setup/readme.md)** - Arduino-based thermocouple reading system (if applicable)
- **Tank Pi Setup** - Ultrasonic sensor configuration and tank monitoring (documentation TBD)
- **Display Pi Setup** - Pygame-based fullscreen display configuration (documentation TBD)

### Server & Web

- **[Web API Documentation](design/)** - WordPress API endpoints, database schema, and integration details
- **Configuration Guide** - See [Configuration & Secrets](#configuration--secrets) below

## Repository Structure

```
sugar_house_monitor/
├── config/                # Environment files (gitignored, see config/example/)
├── data/                  # Local & server SQLite databases, logs (gitignored)
├── design/                # Planning docs, database/API references
├── real_data/             # CSV files for debug replay mode
├── scripts/               # Python services and utilities
│   ├── pump_pi_setup/     # Pump Pi systemd services and setup scripts
│   ├── thermocouple_arduino_setup/  # Arduino thermocouple configuration
│   ├── main_tank.py       # Tank Pi main service
│   ├── main_pump.py       # Pump Pi monolith (legacy, use microservices)
│   ├── pump_controller.py # Pump controller microservice
│   ├── adc_service.py     # MCP3008 ADC reader microservice
│   ├── vacuum_service.py  # Vacuum averaging microservice
│   ├── upload_service.py  # Event upload microservice
│   ├── led_controller.py  # LED status indicator microservice
│   └── main_display.py    # Display Pi pygame service
└── web/                   # Frontend UI and PHP API endpoints
    ├── index.html         # Main web interface
    ├── api/               # PHP ingest/export/history endpoints
    ├── data/              # Generated status JSON files
    └── js/                # Frontend JavaScript (app.js)
```

## Configuration & Secrets

All runtime configuration is controlled via `.env` files in the `config/` directory. Templates are provided in `config/example/`.

### Quick Start

1. **Generate shared API key**:
   ```bash
   python3 scripts/gen_credentials.py
   ```
   This creates `config/server.env`, `config/tank_pi.env`, and `config/pump_pi.env` with a secure shared API key.

2. **Copy config to each device**:
   ```bash
   # Example for Pump Pi
   scp config/pump_pi.env pump@pumppi:/home/pump/sugar_house_monitor/config/
   ```

3. **Edit device-specific settings** as needed (sensor calibration, GPIO pins, upload intervals, etc.)

### Key Environment Variables

| File | Key Variables | Purpose |
|------|---------------|---------|
| `tank_pi.env` | `API_BASE_URL`, `API_KEY` | Server endpoint and authentication |
| | `DEBUG_TANK`, `SYNTHETIC_CLOCK_MULTIPLIER` | CSV replay mode for testing |
| `pump_pi.env` | `PUMP_CONTROL_PIN`, `STATUS_LED_RED_PIN`, `STATUS_LED_GREEN_PIN` | GPIO pin assignments |
| | `ADC_STALE_SECONDS`, `ADC_STALE_FATAL_SECONDS` | Safety timeout thresholds |
| `server.env` | `TANK_DB_PATH`, `PUMP_DB_PATH`, `STATUS_JSON_PATH` | Database and output paths |

See individual subsystem documentation for complete configuration references.

## Installation

### Tank Pi Setup

1. Enable hardware interfaces:
   ```bash
   sudo raspi-config nonint do_serial_cons 1
   sudo raspi-config nonint do_serial_hw 0
   sudo raspi-config nonint do_i2c 0
   ```

2. Enable secondary UART in `/boot/firmware/config.txt`:
   ```
   enable_uart=1
   dtoverlay=uart5   # TX: GPIO12 / RX: GPIO13
   ```

3. Copy `config/example/tank_pi.env` to `config/tank_pi.env` and configure

4. Run `python3 scripts/main_tank.py`

### Pump Pi Setup

See **[Pump Pi Setup Documentation](scripts/pump_pi_setup/readme.md)** for complete installation instructions including:
- SPI interface enablement
- Python virtual environment setup
- Systemd service installation
- LED wiring and GPIO configuration
- Safety interlocks and state machine behavior

### Server Setup (WordPress)

1. Clone repository on server:
   ```bash
   cd ~/git
   git clone https://github.com/jeremymatt/sugar_house_monitor
   ```

2. Symlink web directory into WordPress:
   ```bash
   ln -s ~/git/sugar_house_monitor/web ~/mattsmaplesyrup.com/sugar_house_monitor
   ```

3. Copy and configure `config/example/server.env` to `config/server.env`

## Debug Mode (CSV Replay)

Tank Pi supports replaying historical CSV data for testing without hardware:

1. Set `DEBUG_TANK=true` and `DEBUG_RELEASER=true` in `config/tank_pi.env`
2. Configure CSV paths: `BROOKSIDE_CSV`, `ROADSIDE_CSV`, `PUMP_EVENTS_CSV`
3. Set `RESET_ON_DEBUG_START=true` to wipe databases before replay
4. Adjust playback speed with `SYNTHETIC_CLOCK_MULTIPLIER` (e.g., 10.0 for 10x speed)
5. Run `python3 scripts/main_tank.py`

The service will serve a local web UI at `http://<tank-pi>:<LOCAL_HTTP_PORT>/` and upload to the server API simultaneously.

## Development

### Adding New Sensors

1. Create a new service in `scripts/` (follow microservice pattern)
2. Add shared memory cache for IPC (see `adc_cache.py`, `pump_state_cache.json`)
3. Create systemd service file in `scripts/{subsystem}_setup/systemd/`
4. Update `process_status.py` to include new sensor data in status JSON
5. Add web UI components in `web/js/app.js`

### Testing

- **Unit tests**: (test infrastructure TBD)
- **Integration tests**: Use CSV replay mode on Tank Pi
- **Hardware tests**: See subsystem documentation for device-specific testing procedures

## License

(License information TBD)

## Credits

Developed by Jeremy Matt for Matt's Maple Syrup production monitoring.
