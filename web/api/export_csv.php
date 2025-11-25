<?php
require_once __DIR__ . '/common.php';

// Simple CSV export endpoint.
// Query: type = brookside | roadside | pump | evaporator | vacuum

$env = require_server_env();

$tankDbPath = resolve_repo_path($env['TANK_DB_PATH'] ?? '');
$pumpDbPath = resolve_repo_path($env['PUMP_DB_PATH'] ?? '');
$evapDbPath = resolve_repo_path($env['EVAPORATOR_DB_PATH'] ?? 'data/evaporator.db');
$vacDbPath  = resolve_repo_path($env['VACUUM_DB_PATH'] ?? $pumpDbPath);

$type = $_GET['type'] ?? '';
$type = strtolower($type);

function send_csv_headers(string $filename): void {
    header('Content-Type: text/csv');
    header('Content-Disposition: attachment; filename="' . $filename . '"');
}

function dt_parts(string $iso): array {
    $ts = strtotime($iso);
    return [
        'yr'  => $ts ? intval(date('Y', $ts)) : '',
        'mo'  => $ts ? intval(date('n', $ts)) : '',
        'day' => $ts ? intval(date('j', $ts)) : '',
        'hr'  => $ts ? intval(date('G', $ts)) : '',
        'm'   => $ts ? intval(date('i', $ts)) : '',
        's'   => $ts ? intval(date('s', $ts)) : '',
        'ts'  => $ts,
    ];
}

if ($type === 'brookside' || $type === 'roadside') {
    $tankId = $type;
    $conn = connect_sqlite($tankDbPath);
    $stmt = $conn->prepare(
        'SELECT source_timestamp, surf_dist, depth, volume_gal
         FROM tank_readings
         WHERE tank_id = :tank
         ORDER BY source_timestamp'
    );
    $stmt->execute([':tank' => $tankId]);

    send_csv_headers("{$tankId}.csv");
    $out = fopen('php://output', 'w');
    fputcsv($out, ['Unnamed: 0','timestamp','yr','mo','day','hr','m','s','surf_dist','depth','gal']);
    $idx = 0;
    foreach ($stmt as $row) {
        $parts = dt_parts($row['source_timestamp']);
        fputcsv($out, [
            $idx++,
            $row['source_timestamp'],
            $parts['yr'],
            $parts['mo'],
            $parts['day'],
            $parts['hr'],
            $parts['m'],
            $parts['s'],
            $row['surf_dist'],
            $row['depth'],
            $row['volume_gal'],
        ]);
    }
    fclose($out);
    exit;
}

if ($type === 'pump') {
    $conn = connect_sqlite($pumpDbPath);
    $stmt = $conn->query(
        'SELECT source_timestamp, event_type, pump_run_time_s, pump_interval_s, gallons_per_hour
         FROM pump_events
         ORDER BY source_timestamp'
    );
    send_csv_headers('pump_times.csv');
    $out = fopen('php://output', 'w');
    fputcsv($out, ['Time','Pump Event','Pump Run Time','Pump Interval','Gallons Per Hour']);
    foreach ($stmt as $row) {
        $ts = $row['source_timestamp'];
        $formatted = $ts ? date('Y-m-d-H:i:s', strtotime($ts)) : '';
        fputcsv($out, [
            $formatted,
            $row['event_type'],
            $row['pump_run_time_s'],
            $row['pump_interval_s'],
            $row['gallons_per_hour'],
        ]);
    }
    fclose($out);
    exit;
}

if ($type === 'evaporator') {
    $conn = connect_sqlite($evapDbPath);
    $conn->exec(
        'CREATE TABLE IF NOT EXISTS evaporator_flow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_timestamp TEXT NOT NULL,
            draw_off_tank TEXT,
            pump_in_tank TEXT,
            draw_off_flow_gph REAL,
            pump_in_flow_gph REAL,
            pump_flow_gph REAL,
            brookside_flow_gph REAL,
            roadside_flow_gph REAL,
            evaporator_flow_gph REAL,
            created_at TEXT NOT NULL
        )'
    );
    $stmt = $conn->query(
        'SELECT sample_timestamp, draw_off_tank, draw_off_flow_gph,
                pump_in_tank, pump_in_flow_gph, evaporator_flow_gph
         FROM evaporator_flow
         ORDER BY sample_timestamp'
    );
    send_csv_headers('evaporator.csv');
    $out = fopen('php://output', 'w');
    fputcsv($out, [
        'timestamp',
        'Draw_off_tank',
        'Draw_off_flow_rate',
        'Pump_in_tank',
        'Pump_in_flow_rate',
        'Evaporator_flow',
    ]);
    foreach ($stmt as $row) {
        fputcsv($out, [
            $row['sample_timestamp'],
            $row['draw_off_tank'],
            $row['draw_off_flow_gph'],
            $row['pump_in_tank'],
            $row['pump_in_flow_gph'],
            $row['evaporator_flow_gph'],
        ]);
    }
    fclose($out);
    exit;
}

if ($type === 'vacuum') {
    $conn = connect_sqlite($vacDbPath);
    $conn->exec(
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
    $stmt = $conn->query(
        'SELECT source_timestamp, reading_inhg
         FROM vacuum_readings
         ORDER BY source_timestamp'
    );
    send_csv_headers('vacuum.csv');
    $out = fopen('php://output', 'w');
    fputcsv($out, ['timestamp','Vacuum']);
    foreach ($stmt as $row) {
        fputcsv($out, [
            $row['source_timestamp'],
            $row['reading_inhg'],
        ]);
    }
    fclose($out);
    exit;
}

respond_error('Unknown export type', 400);
