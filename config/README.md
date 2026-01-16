# Configuration files

Real runtime secrets live in this folder but are ignored by git. Copy or rename the example files in `config/example/` to the filenames below and customize them for each device:

- `config/server.env`
- `config/tank_pi.env`
- `config/pump_pi.env`
- `config/display_pi.env`

Python modules look for `config/` relative to the repo root by default. You can point services somewhere else by exporting `SUGAR_CONFIG_DIR=/path/to/config` before execution.

Never commit populated `.env` files—`config/*.env` is gitignored on purpose. If you need distinct configs for different deployments, create sibling folders (e.g., `config/prod/`, `config/lab/`) and symlink/rename the one you want into place.
