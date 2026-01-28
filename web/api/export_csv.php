<?php
require_once __DIR__ . '/common.php';

// Simple CSV export endpoint.
// Query: type = brookside | roadside | pump | evaporator | vacuum | o2 | stacktemp

$env = require_server_env();

$tankDbPath = resolve_repo_path($env['TANK_DB_PATH'] ?? '');
$pumpDbPath = resolve_repo_path($env['PUMP_DB_PATH'] ?? '');
$evapDbPath = resolve_repo_path($env['EVAPORATOR_DB_PATH'] ?? 'data/evaporator.db');
$vacDbPath  = resolve_repo_path($env['VACUUM_DB_PATH'] ?? $pumpDbPath);
$o2DbPath  = resolve_repo_path($env['O2_DB_PATH'] ?? 'data/o2_server.db');
$stackDbPath = resolve_repo_path($env['STACK_TEMP_DB_PATH'] ?? $vacDbPath);

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

function format_iso_tz(?string $iso, DateTimeZone $tz): string {
    if (!$iso) {
        return '';
    }
    try {
        $dt = new DateTime($iso);
        $dt->setTimezone($tz);
        return $dt->format(DATE_ATOM);
    } catch (Exception $e) {
        return '';
    }
}

$utcTz = new DateTimeZone('UTC');
$estTz = new DateTimeZone('America/New_York');

function table_has_column(PDO $conn, string $table, string $column): bool {
    // Best-effort sanitation of table name since PRAGMA can't use bound params.
    $table_safe = preg_replace('/[^A-Za-z0-9_]/', '', $table);
    $stmt = $conn->query('PRAGMA table_info(' . $table_safe . ')');
    if ($stmt === false) {
        return false;
    }
    $rows = $stmt->fetchAll(PDO::FETCH_ASSOC);
    foreach ($rows as $row) {
        if (isset($row['name']) && strtolower($row['name']) === strtolower($column)) {
            return true;
        }
    }
    return false;
}

if ($type === 'brookside' || $type === 'roadside') {
    $tankId = $type;
    $conn = connect_sqlite($tankDbPath);
    $hasOutlier = table_has_column($conn, 'tank_readings', 'depth_outlier');
    $selectCols = 'source_timestamp, surf_dist, depth';
    if ($hasOutlier) {
        $selectCols .= ', depth_outlier';
    } else {
        $selectCols .= ', NULL AS depth_outlier';
    }
    $selectCols .= ', volume_gal, flow_gph';
    $stmt = $conn->prepare(
        'SELECT ' . $selectCols . '
         FROM tank_readings
         WHERE tank_id = :tank
         ORDER BY source_timestamp'
    );
    $stmt->execute([':tank' => $tankId]);

    send_csv_headers("{$tankId}.csv");
    $out = fopen('php://output', 'w');
    fputcsv($out, ['Unnamed: 0','timestamp','timestamp_utc','timestamp_est','yr','mo','day','hr','m','s','surf_dist','depth','is_outlier','gal','flow_gph']);
    $idx = 0;
    foreach ($stmt as $row) {
        $parts = dt_parts($row['source_timestamp']);
        $tsUtc = format_iso_tz($row['source_timestamp'], $utcTz);
        $tsEst = format_iso_tz($row['source_timestamp'], $estTz);
        fputcsv($out, [
            $idx++,
            $row['source_timestamp'],
            $tsUtc,
            $tsEst,
            $parts['yr'],
            $parts['mo'],
            $parts['day'],
            $parts['hr'],
            $parts['m'],
            $parts['s'],
            $row['surf_dist'],
            $row['depth'],
            $row['depth_outlier'],
            $row['volume_gal'],
            $row['flow_gph'],
        ]);
    }
    fclose($out);
    exit;
}

if ($type === 'stack' || $type === 'stacktemp' || $type === 'stacktemps') {
    $conn = connect_sqlite($stackDbPath);
    $conn->exec(
        'CREATE TABLE IF NOT EXISTS stack_temperatures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_temp_f REAL,
            ambient_temp_f REAL,
            source_timestamp TEXT NOT NULL,
            received_at TEXT NOT NULL,
            UNIQUE(source_timestamp)
        )'
    );
    $stmt = $conn->query(
        'SELECT source_timestamp, stack_temp_f, ambient_temp_f
         FROM stack_temperatures
         ORDER BY source_timestamp'
    );
    send_csv_headers('stack_temperatures.csv');
    $out = fopen('php://output', 'w');
    fputcsv($out, ['timestamp','Stack Temp (F)','Ambient Temp (F)']);
    foreach ($stmt as $row) {
        fputcsv($out, [
            $row['source_timestamp'],
            $row['stack_temp_f'],
            $row['ambient_temp_f'],
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
    fputcsv($out, ['Time','timestamp_utc','timestamp_est','Pump Event','Pump Run Time','Pump Interval','Gallons Per Hour']);
    foreach ($stmt as $row) {
        $ts = $row['source_timestamp'];
        $formatted = '';
        if ($ts) {
            try {
                $dt = new DateTime($ts);
                $dt->setTimezone($utcTz);
                $formatted = $dt->format('Y-m-d-H:i:s');
            } catch (Exception $e) {
                $formatted = $ts;
            }
        }
        $tsUtc = format_iso_tz($ts, $utcTz);
        $tsEst = format_iso_tz($ts, $estTz);
        fputcsv($out, [
            $formatted,
            $tsUtc,
            $tsEst,
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
        'timestamp_utc',
        'timestamp_est',
        'Draw_off_tank',
        'Draw_off_flow_rate',
        'Pump_in_tank',
        'Pump_in_flow_rate',
        'Evaporator_flow',
    ]);
    foreach ($stmt as $row) {
        $tsUtc = format_iso_tz($row['sample_timestamp'], $utcTz);
        $tsEst = format_iso_tz($row['sample_timestamp'], $estTz);
        fputcsv($out, [
            $row['sample_timestamp'],
            $tsUtc,
            $tsEst,
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

if ($type === 'o2' || $type === 'oh_two') {
    $conn = connect_sqlite($o2DbPath);
    $conn->exec(
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
    $stmt = $conn->query(
        'SELECT source_timestamp, o2_percent
         FROM o2_readings
         ORDER BY source_timestamp'
    );
    send_csv_headers('o2.csv');
    $out = fopen('php://output', 'w');
    fputcsv($out, ['timestamp','O2 (%)']);
    foreach ($stmt as $row) {
        fputcsv($out, [
            $row['source_timestamp'],
            $row['o2_percent'],
        ]);
    }
    fclose($out);
    exit;
}

respond_error('Unknown export type', 400);
