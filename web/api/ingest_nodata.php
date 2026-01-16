<?php
require_once __DIR__ . '/common.php';

ensure_post();
$env = require_server_env();
ensure_api_key($env);
$payload = decode_json_body();

$stream = $payload['stream'] ?? null;
if (!in_array($stream, ['tank', 'pump'], true)) {
    respond_error('Invalid or missing stream (expected "tank" or "pump")', 400);
}

// Route tank heartbeats to the tank DB and pump heartbeats to the pump DB.
$dbPath = $stream === 'pump'
    ? ($env['PUMP_DB_PATH'] ?? $env['TANK_DB_PATH'])
    : $env['TANK_DB_PATH'];

$db = connect_sqlite(resolve_repo_path($dbPath));
ensure_monitor_table($db);
$now = gmdate('c');
update_monitor($db, $stream, $now);

$storage = load_storage_status($env);
$storageUpdated = false;
$key = $stream === 'pump' ? 'pump_pi' : 'tank_pi';
$storageUpdated = apply_storage_entry($storage, $key, $payload, $now) || $storageUpdated;

$serverPath = $env['DISK_USAGE_PATH'] ?? REPO_ROOT;
$serverTotal = @disk_total_space($serverPath);
$serverFree = @disk_free_space($serverPath);
if ($serverTotal !== false && $serverFree !== false) {
    $storage['server'] = [
        'total_bytes' => (int) $serverTotal,
        'used_bytes' => (int) max(0, $serverTotal - $serverFree),
        'free_bytes' => (int) $serverFree,
        'path' => $serverPath,
        'updated_at' => $now,
    ];
    $storageUpdated = true;
}
if ($storageUpdated) {
    $storage['generated_at'] = $now;
    save_storage_status($env, $storage);
}

trigger_status_refresh();

respond_json([
    'status' => 'ok',
    'stream' => $stream,
    'received_at' => $now,
]);
