<?php
require_once __DIR__ . '/common.php';

ensure_post();
$env = require_server_env();
ensure_api_key($env);
$payload = decode_json_body();

$records = $payload['events'] ?? $payload;
if (!is_array($records)) {
    respond_error('Expected an array of events', 400);
}

$db = connect_sqlite(resolve_repo_path($env['PUMP_DB_PATH']));
ensure_monitor_table($db);
$db->exec(
    'CREATE TABLE IF NOT EXISTS pump_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        source_timestamp TEXT NOT NULL,
        pump_run_time_s REAL,
        pump_interval_s REAL,
        gallons_per_hour REAL,
        raw_payload TEXT,
        received_at TEXT NOT NULL,
        UNIQUE(event_type, source_timestamp)
    )'
);
$db->exec('CREATE INDEX IF NOT EXISTS idx_pump_events_source_ts ON pump_events(source_timestamp)');

$insert = $db->prepare(
    'INSERT INTO pump_events (
        event_type, source_timestamp, pump_run_time_s, pump_interval_s,
        gallons_per_hour, raw_payload, received_at
    ) VALUES (
        :event_type, :source_timestamp, :pump_run_time_s, :pump_interval_s,
        :gallons_per_hour, :raw_payload, :received_at
    )
    ON CONFLICT(event_type, source_timestamp) DO UPDATE SET
        pump_run_time_s=excluded.pump_run_time_s,
        pump_interval_s=excluded.pump_interval_s,
        gallons_per_hour=excluded.gallons_per_hour,
        raw_payload=excluded.raw_payload,
        received_at=excluded.received_at'
);

$accepted = 0;
$db->beginTransaction();
$now = gmdate('c');
foreach ($records as $record) {
    if (!is_array($record)) {
        continue;
    }
    $timestamp = $record['source_timestamp'] ?? $record['Time'] ?? null;
    $eventType = $record['event_type'] ?? $record['Pump_Event'] ?? null;
    if (!$timestamp || !$eventType) {
        continue;
    }

    $insert->execute([
        ':event_type' => $eventType,
        ':source_timestamp' => $timestamp,
        ':pump_run_time_s' => $record['pump_run_time_s'] ?? $record['Pump_Run_Time'] ?? null,
        ':pump_interval_s' => $record['pump_interval_s'] ?? $record['Pump_Interval'] ?? null,
        ':gallons_per_hour' => $record['gallons_per_hour'] ?? $record['Gallons_Per_Hour'] ?? null,
        ':raw_payload' => json_encode($record),
        ':received_at' => $now,
    ]);

    $accepted += $insert->rowCount();
}
$db->commit();

trigger_status_refresh();

update_monitor($db, 'pump', $now);

$last = null;
$stmt = $db->query('SELECT MAX(source_timestamp) AS last_timestamp FROM pump_events');
if ($row = $stmt->fetch()) {
    $last = $row['last_timestamp'];
}

respond_json([
    'status' => 'ok',
    'accepted' => $accepted,
    'last_timestamp' => $last,
]);
