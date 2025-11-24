<?php
require_once __DIR__ . '/common.php';

ensure_post();
$env = require_server_env();
ensure_api_key($env);

$tankPath = resolve_repo_path($env['TANK_DB_PATH']);
$pumpPath = resolve_repo_path($env['PUMP_DB_PATH']);
$evapPath = resolve_repo_path($env['EVAPORATOR_DB_PATH'] ?? 'data/evaporator.db');
$statusPath = resolve_repo_path($env['STATUS_JSON_PATH']);

function safe_unlink(string $path): void {
    if (file_exists($path)) {
        unlink($path);
    }
}

function safe_unlink_sqlite(string $basePath): void {
    safe_unlink($basePath);
    safe_unlink($basePath . '-wal');
    safe_unlink($basePath . '-shm');
}

safe_unlink_sqlite($tankPath);
safe_unlink_sqlite($pumpPath);
safe_unlink_sqlite($evapPath);

if (file_exists($statusPath)) {
    file_put_contents($statusPath, json_encode([
        'generated_at' => null,
        'tanks' => new stdClass(),
        'pump' => null,
    ], JSON_PRETTY_PRINT));
}

// Clear derived status JSON files to avoid stale displays.
foreach (glob(dirname($statusPath) . '/status_*.json') as $file) {
    safe_unlink($file);
}

respond_json(['status' => 'ok', 'message' => 'Server state reset']);
