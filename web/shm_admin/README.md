# SHM Admin (Display Settings)

This page lives at `/sugar_house_monitor/shm_admin/` and is protected with HTTP Basic Auth.

## Adding users
1. Copy `config/example/shm_admin.ini` to `config/shm_admin.ini`.
2. Generate a password hash:
   ```
   php -r "echo password_hash('your-password', PASSWORD_DEFAULT) . PHP_EOL;"
   ```
3. Add or update a line in `config/shm_admin.ini`:
   ```
   username=the_hash_from_step_2
   ```
4. Add more users by adding more lines (one per user).

If you change the file, reload the page and the new credentials will take effect immediately.
