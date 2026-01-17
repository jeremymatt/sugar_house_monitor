<?php
require_once __DIR__ . '/common.php';

ensure_post();
$env = require_server_env();
ensure_api_key($env);
$payload = decode_json_body();

$records = $payload['readings'] ?? $payload;
if (!is_array($records)) {
    respond_error('Expected an array of readings', 400);
}

$isAssocRecord = isset($records['stack_temp_f']) || isset($records['ambient_temp_f']) || isset($records['hot_junction']) || isset($records['cold_junction']);
if ($isAssocRecord) {
    $records = [$records];
}

$dbPath = resolve_repo_path(
    $env['STACK_TEMP_DB_PATH']
        ?? $env['VACUUM_DB_PATH']
        ?? $env['PUMP_DB_PATH']
        ?? $env['TANK_DB_PATH']
);
$db = connect_sqlite($dbPath);

$db->exec(
    'CREATE TABLE IF NOT EXISTS stack_temperatures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stack_temp_f REAL,
        ambient_temp_f REAL,
        source_timestamp TEXT NOT NULL,
        received_at TEXT NOT NULL,
        UNIQUE(source_timestamp)
    )'
);
$db->exec('CREATE INDEX IF NOT EXISTS idx_stack_temperatures_source_ts ON stack_temperatures(source_timestamp)');

$insert = $db->prepare(
    'INSERT INTO stack_temperatures (
        stack_temp_f, ambient_temp_f, source_timestamp, received_at
    ) VALUES (
        :stack_temp_f, :ambient_temp_f, :source_timestamp, :received_at
    )
    ON CONFLICT(source_timestamp) DO UPDATE SET
        stack_temp_f=excluded.stack_temp_f,
        ambient_temp_f=excluded.ambient_temp_f,
        received_at=excluded.received_at'
);

$accepted = 0;
$db->beginTransaction();
$now = gmdate('c');
foreach ($records as $record) {
    if (!is_array($record)) {
        continue;
    }
    $ts = $record['source_timestamp'] ?? $record['timestamp'] ?? $record['ts'] ?? null;
    if (!$ts) {
        $ts = gmdate('c');
    }
    $stackVal = $record['stack_temp_f'] ?? $record['hot_junction_f'] ?? $record['hot_junction'] ?? null;
    $ambientVal = $record['ambient_temp_f'] ?? $record['cold_junction_f'] ?? $record['cold_junction'] ?? null;
    if ($stackVal === null && $ambientVal === null) {
        continue;
    }
    $insert->execute([
        ':stack_temp_f' => $stackVal,
        ':ambient_temp_f' => $ambientVal,
        ':source_timestamp' => $ts,
        ':received_at' => $now,
    ]);
    $accepted += $insert->rowCount();
}
$db->commit();

trigger_status_refresh();

respond_json([
    'status' => 'ok',
    'accepted' => $accepted,
]);
