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

$db = connect_sqlite(resolve_repo_path($env['TANK_DB_PATH']));
ensure_monitor_table($db);

function ensure_column(PDO $db, string $table, string $column, string $definition): void {
    $stmt = $db->query("PRAGMA table_info($table)");
    $columns = [];
    foreach ($stmt as $row) {
        $columns[] = $row['name'];
    }
    if (!in_array($column, $columns, true)) {
        $db->exec("ALTER TABLE {$table} ADD COLUMN {$column} {$definition}");
    }
}

$db->exec(
    'CREATE TABLE IF NOT EXISTS tank_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tank_id TEXT NOT NULL,
        source_timestamp TEXT NOT NULL,
        surf_dist REAL,
        depth REAL,
        depth_outlier INTEGER,
        volume_gal REAL,
        max_volume_gal REAL,
        level_percent REAL,
        flow_gph REAL,
        eta_full TEXT,
        eta_empty TEXT,
        time_to_full_min REAL,
        time_to_empty_min REAL,
        raw_payload TEXT,
        received_at TEXT NOT NULL,
        UNIQUE(tank_id, source_timestamp)
    )'
);
$db->exec('CREATE INDEX IF NOT EXISTS idx_tank_readings_source_ts ON tank_readings(source_timestamp)');
ensure_column($db, 'tank_readings', 'max_volume_gal', 'REAL');
ensure_column($db, 'tank_readings', 'level_percent', 'REAL');
ensure_column($db, 'tank_readings', 'depth_outlier', 'INTEGER');

$insert = $db->prepare(
    'INSERT INTO tank_readings (
        tank_id, source_timestamp, surf_dist, depth, depth_outlier, volume_gal, max_volume_gal,
        level_percent, flow_gph, eta_full, eta_empty, time_to_full_min,
        time_to_empty_min, raw_payload, received_at
    ) VALUES (
        :tank_id, :source_timestamp, :surf_dist, :depth, :depth_outlier, :volume_gal, :max_volume_gal,
        :level_percent, :flow_gph, :eta_full, :eta_empty, :time_to_full_min,
        :time_to_empty_min, :raw_payload, :received_at
    )
    ON CONFLICT(tank_id, source_timestamp) DO UPDATE SET
        surf_dist=excluded.surf_dist,
        depth=excluded.depth,
        depth_outlier=excluded.depth_outlier,
        volume_gal=excluded.volume_gal,
        max_volume_gal=excluded.max_volume_gal,
        level_percent=excluded.level_percent,
        flow_gph=excluded.flow_gph,
        eta_full=excluded.eta_full,
        eta_empty=excluded.eta_empty,
        time_to_full_min=excluded.time_to_full_min,
        time_to_empty_min=excluded.time_to_empty_min,
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
    $tankId = $record['tank_id'] ?? null;
    $timestamp = $record['source_timestamp'] ?? $record['timestamp'] ?? null;
    if (!$tankId || !$timestamp) {
        continue;
    }

    $insert->execute([
        ':tank_id' => $tankId,
        ':source_timestamp' => $timestamp,
        ':surf_dist' => $record['surf_dist'] ?? null,
        ':depth' => $record['depth'] ?? null,
        ':depth_outlier' => array_key_exists('depth_outlier', $record) ? $record['depth_outlier'] : null,
        ':volume_gal' => $record['volume_gal'] ?? $record['volume'] ?? null,
        ':max_volume_gal' => $record['max_volume_gal'] ?? null,
        ':level_percent' => $record['level_percent'] ?? null,
        ':flow_gph' => $record['flow_gph'] ?? null,
        ':eta_full' => $record['eta_full'] ?? null,
        ':eta_empty' => $record['eta_empty'] ?? null,
        ':time_to_full_min' => $record['time_to_full_min'] ?? null,
        ':time_to_empty_min' => $record['time_to_empty_min'] ?? null,
        ':raw_payload' => json_encode($record),
        ':received_at' => $now,
    ]);
    $accepted += $insert->rowCount();
}
$db->commit();

trigger_status_refresh();

$latest = [];
$stmt = $db->query('SELECT tank_id, MAX(source_timestamp) AS last_timestamp FROM tank_readings GROUP BY tank_id');
foreach ($stmt as $row) {
    $latest[$row['tank_id']] = $row['last_timestamp'];
}

update_monitor($db, 'tank', $now);

$storage = load_storage_status($env);
$storageUpdated = apply_storage_entry($storage, 'tank_pi', $payload, $now);
if ($storageUpdated) {
    $storage['generated_at'] = $now;
    save_storage_status($env, $storage);
}

respond_json([
    'status' => 'ok',
    'accepted' => $accepted,
    'last_timestamps' => $latest,
]);
