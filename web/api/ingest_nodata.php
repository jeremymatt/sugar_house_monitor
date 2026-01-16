<?php
require_once __DIR__ . '/common.php';

function normalize_int($value): ?int {
    if ($value === null || $value === '') {
        return null;
    }
    if (is_numeric($value)) {
        $intVal = (int) $value;
        return $intVal >= 0 ? $intVal : null;
    }
    return null;
}

function storage_status_path(array $env): string {
    $statusPath = resolve_repo_path($env['STATUS_JSON_PATH'] ?? 'web/data/status.json');
    return rtrim(dirname($statusPath), DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR . 'status_storage.json';
}

function read_json_file(string $path): array {
    if (!file_exists($path)) {
        return [];
    }
    $raw = file_get_contents($path);
    if ($raw === false) {
        return [];
    }
    $data = json_decode($raw, true);
    return is_array($data) ? $data : [];
}

function write_json_file(string $path, array $payload): void {
    $dir = dirname($path);
    if (!is_dir($dir)) {
        mkdir($dir, 0775, true);
    }
    $tmp = tempnam($dir, 'tmp');
    if ($tmp === false) {
        return;
    }
    file_put_contents($tmp, json_encode($payload), LOCK_EX);
    rename($tmp, $path);
}

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

$storagePath = storage_status_path($env);
$storage = read_json_file($storagePath);
$storage['generated_at'] = $now;

$diskTotal = normalize_int($payload['disk_total_bytes'] ?? null);
$diskUsed = normalize_int($payload['disk_used_bytes'] ?? null);
$diskFree = normalize_int($payload['disk_free_bytes'] ?? null);
$diskPath = isset($payload['disk_path']) ? trim((string) $payload['disk_path']) : '';

if ($diskTotal !== null && ($diskUsed !== null || $diskFree !== null)) {
    if ($diskUsed === null && $diskFree !== null) {
        $diskUsed = max(0, $diskTotal - $diskFree);
    }
    if ($diskFree === null && $diskUsed !== null) {
        $diskFree = max(0, $diskTotal - $diskUsed);
    }
    $key = $stream === 'pump' ? 'pump_pi' : 'tank_pi';
    $storage[$key] = [
        'total_bytes' => $diskTotal,
        'used_bytes' => $diskUsed,
        'free_bytes' => $diskFree,
        'path' => $diskPath ?: null,
        'updated_at' => $now,
    ];
}

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
}
write_json_file($storagePath, $storage);

trigger_status_refresh();

respond_json([
    'status' => 'ok',
    'stream' => $stream,
    'received_at' => $now,
]);
