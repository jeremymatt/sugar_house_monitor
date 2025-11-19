<?php
require_once __DIR__ . '/common.php';

ensure_post();
$env = require_server_env();
ensure_api_key($env);

$tankPath = resolve_repo_path($env['TANK_DB_PATH']);
$pumpPath = resolve_repo_path($env['PUMP_DB_PATH']);
$statusPath = resolve_repo_path($env['STATUS_JSON_PATH']);

function safe_unlink(string $path): void {
    if (file_exists($path)) {
        unlink($path);
    }
}

safe_unlink($tankPath);
safe_unlink($pumpPath);

if (file_exists($statusPath)) {
    file_put_contents($statusPath, json_encode([
        'generated_at' => null,
        'tanks' => new stdClass(),
        'pump' => null,
    ], JSON_PRETTY_PRINT));
}

respond_json(['status' => 'ok', 'message' => 'Server state reset']);
