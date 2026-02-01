# Sugar House Monitor

Live and simulated monitoring for tank levels, transfer pump, stack temperature, vacuum, and O2 with a WordPress-hosted UI.

## Subsystem documentation
- Tank Pi (ultrasonic + local UI): scripts/tank_pi_setup/readme.md
- Pump Pi (relay + ADC + watchdog + LED): scripts/pump_pi_setup/readme.md
- Display Pi (full-screen status board): scripts/display_pi_setup/readme.md
- O2 Pi (MCP3008 sampling + upload): scripts/oh_two_pi_setup/readme.md
- Thermocouple Arduino (MCP9600 + LCD): scripts/thermocouple_arduino_setup/readme.md
- Server (WordPress host + ingest API): scripts/setup_server/readme.md

## Quick orientation
- web/ hosts the UI and API endpoints.
- scripts/ contains the services and setup helpers.
- config/example/ has env templates; copy to config/*.env and edit.

## Credentials and config
Run once on a trusted machine to create env files and a shared API key:

```
python3 scripts/gen_credentials.py
```

Then copy the generated config/*.env files to each device and follow the subsystem setup docs above.
