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

$db = connect_sqlite(resolve_repo_path($env['VACUUM_DB_PATH'] ?? $env['PUMP_DB_PATH'] ?? $env['TANK_DB_PATH']));

$db->exec(
    'CREATE TABLE IF NOT EXISTS vacuum_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reading_inhg REAL,
        source_timestamp TEXT NOT NULL,
        received_at TEXT NOT NULL,
        sent_to_server INTEGER DEFAULT 0,
        acked_by_server INTEGER DEFAULT 0,
        UNIQUE(source_timestamp)
    )'
);
$db->exec('CREATE INDEX IF NOT EXISTS idx_vacuum_readings_source_ts ON vacuum_readings(source_timestamp)');

$insert = $db->prepare(
    'INSERT INTO vacuum_readings (
        reading_inhg, source_timestamp, received_at
    ) VALUES (
        :reading_inhg, :source_timestamp, :received_at
    )
    ON CONFLICT(source_timestamp) DO UPDATE SET
        reading_inhg=excluded.reading_inhg,
        received_at=excluded.received_at'
);

$accepted = 0;
$db->beginTransaction();
$now = gmdate('c');
foreach ($records as $record) {
    if (!is_array($record)) {
        continue;
    }
    $ts = $record['source_timestamp'] ?? $record['timestamp'] ?? null;
    if (!$ts) {
        continue;
    }
    $insert->execute([
        ':reading_inhg' => $record['reading_inhg'] ?? null,
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
