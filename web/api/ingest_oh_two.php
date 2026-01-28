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

$isAssocRecord = isset($records['o2_percent']) || isset($records['raw_value']) || isset($records['volts']);
if ($isAssocRecord) {
    $records = [$records];
}

$dbPath = resolve_repo_path($env['O2_DB_PATH'] ?? $env['PUMP_DB_PATH'] ?? $env['TANK_DB_PATH'] ?? '');
$db = connect_sqlite($dbPath);

$db->exec(
    'CREATE TABLE IF NOT EXISTS o2_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        o2_percent REAL,
        raw_value REAL,
        volts REAL,
        source_timestamp TEXT NOT NULL,
        received_at TEXT NOT NULL,
        UNIQUE(source_timestamp)
    )'
);
$db->exec('CREATE INDEX IF NOT EXISTS idx_o2_readings_source_ts ON o2_readings(source_timestamp)');

$insert = $db->prepare(
    'INSERT INTO o2_readings (
        o2_percent, raw_value, volts, source_timestamp, received_at
    ) VALUES (
        :o2_percent, :raw_value, :volts, :source_timestamp, :received_at
    )
    ON CONFLICT(source_timestamp) DO UPDATE SET
        o2_percent=excluded.o2_percent,
        raw_value=excluded.raw_value,
        volts=excluded.volts,
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
        continue;
    }
    $insert->execute([
        ':o2_percent' => $record['o2_percent'] ?? null,
        ':raw_value' => $record['raw_value'] ?? null,
        ':volts' => $record['volts'] ?? null,
        ':source_timestamp' => $ts,
        ':received_at' => $now,
    ]);
    $accepted += $insert->rowCount();
}
$db->commit();

trigger_status_refresh();
update_monitor($db, 'oh_two', $now);

respond_json([
    'status' => 'ok',
    'accepted' => $accepted,
]);
