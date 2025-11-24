<?php
require_once __DIR__ . '/common.php';

$env = require_server_env();
$dbPath = resolve_repo_path($env['EVAPORATOR_DB_PATH'] ?? 'data/evaporator.db');
$db = connect_sqlite($dbPath);

$minOptions = [0, 100, 200, 300, 400, 500];
$maxOptions = [100, 200, 300, 400, 500, 600];
$windowOptions = [3600, 7200, 14400, 21600, 28800, 43200]; // 1h,2h,4h,6h,8h,12h

function ensure_tables(PDO $db): void {
    $db->exec(
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
    $db->exec(
        'CREATE TABLE IF NOT EXISTS plot_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            y_axis_min REAL NOT NULL,
            y_axis_max REAL NOT NULL,
            window_sec INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )'
    );
    $stmt = $db->query('SELECT 1 FROM plot_settings WHERE id = 1');
    if (!$stmt->fetch()) {
        $now = gmdate('c');
        $db->exec(
            "INSERT INTO plot_settings (id, y_axis_min, y_axis_max, window_sec, updated_at)
             VALUES (1, 200.0, 600.0, 7200, '{$now}')"
        );
    }
}

function load_settings(PDO $db): array {
    $stmt = $db->query('SELECT y_axis_min, y_axis_max, window_sec FROM plot_settings WHERE id = 1');
    if ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
        return [
            'y_axis_min' => (float) $row['y_axis_min'],
            'y_axis_max' => (float) $row['y_axis_max'],
            'window_sec' => (int) $row['window_sec'],
        ];
    }
    return ['y_axis_min' => 200.0, 'y_axis_max' => 600.0, 'window_sec' => 7200];
}

function reconcile_bounds(float $yMin, float $yMax, array $mins, array $maxes): array {
    if ($yMin >= $yMax) {
        foreach ($maxes as $candidate) {
            if ($candidate > $yMin) {
                $yMax = (float) $candidate;
                break;
            }
        }
    }
    if ($yMax <= $yMin) {
        for ($i = count($mins) - 1; $i >= 0; $i--) {
            if ($mins[$i] < $yMax) {
                $yMin = (float) $mins[$i];
                break;
            }
        }
    }
    return [$yMin, $yMax];
}

ensure_tables($db);
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

if ($method === 'POST') {
    ensure_post();
    $payload = decode_json_body();
    $yMin = isset($payload['y_axis_min']) ? floatval($payload['y_axis_min']) : null;
    $yMax = isset($payload['y_axis_max']) ? floatval($payload['y_axis_max']) : null;
    $windowSec = isset($payload['window_sec']) ? intval($payload['window_sec']) : null;

    if ($yMin === null || $yMax === null || $windowSec === null) {
        respond_error('Missing required fields', 400);
    }
    if (!in_array($yMin, $minOptions, true) || !in_array($yMax, $maxOptions, true)) {
        respond_error('Invalid y-axis bounds', 400);
    }
    if (!in_array($windowSec, $windowOptions, true)) {
        respond_error('Invalid window', 400);
    }

    [$yMin, $yMax] = reconcile_bounds($yMin, $yMax, $minOptions, $maxOptions);

    $stmt = $db->prepare(
        'INSERT INTO plot_settings (id, y_axis_min, y_axis_max, window_sec, updated_at)
         VALUES (1, :min, :max, :win, :ts)
         ON CONFLICT(id) DO UPDATE SET
            y_axis_min=excluded.y_axis_min,
            y_axis_max=excluded.y_axis_max,
            window_sec=excluded.window_sec,
            updated_at=excluded.updated_at'
    );
    $stmt->execute([
        ':min' => $yMin,
        ':max' => $yMax,
        ':win' => $windowSec,
        ':ts' => gmdate('c'),
    ]);

    respond_json([
        'status' => 'ok',
        'settings' => [
            'y_axis_min' => $yMin,
            'y_axis_max' => $yMax,
            'window_sec' => $windowSec,
        ],
    ]);
}

$settings = load_settings($db);
$windowSec = $settings['window_sec'];
$windowOverride = isset($_GET['window_sec']) ? intval($_GET['window_sec']) : null;
if ($windowOverride && in_array($windowOverride, $windowOptions, true)) {
    $windowSec = $windowOverride;
}

$latestTs = null;
$stmt = $db->query('SELECT MAX(sample_timestamp) AS max_ts FROM evaporator_flow');
if ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
    $latestTs = $row['max_ts'] ? strtotime($row['max_ts']) : null;
}
$cutoffTs = $latestTs ? $latestTs - $windowSec : (time() - $windowSec);
$cutoffIso = gmdate('c', $cutoffTs);

$historyRows = [];
$stmt = $db->prepare(
    'SELECT sample_timestamp, evaporator_flow_gph, draw_off_tank, pump_in_tank
     FROM evaporator_flow
     WHERE sample_timestamp >= :cutoff
     ORDER BY sample_timestamp'
);
$stmt->execute([':cutoff' => $cutoffIso]);
foreach ($stmt as $row) {
    $historyRows[] = [
        'ts' => $row['sample_timestamp'],
        'evaporator_flow_gph' => $row['evaporator_flow_gph'],
        'draw_off_tank' => $row['draw_off_tank'],
        'pump_in_tank' => $row['pump_in_tank'],
    ];
}

$latest = null;
$stmt = $db->query(
    'SELECT *
     FROM evaporator_flow
     ORDER BY sample_timestamp DESC, id DESC
     LIMIT 1'
);
if ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
    $latest = [
        'sample_timestamp' => $row['sample_timestamp'],
        'draw_off_tank' => $row['draw_off_tank'],
        'pump_in_tank' => $row['pump_in_tank'],
        'draw_off_flow_gph' => $row['draw_off_flow_gph'],
        'pump_in_flow_gph' => $row['pump_in_flow_gph'],
        'pump_flow_gph' => $row['pump_flow_gph'],
        'evaporator_flow_gph' => $row['evaporator_flow_gph'],
        'brookside_flow_gph' => $row['brookside_flow_gph'],
        'roadside_flow_gph' => $row['roadside_flow_gph'],
    ];
}

respond_json([
    'status' => 'ok',
    'settings' => $settings,
    'window_sec_used' => $windowSec,
    'latest' => $latest,
    'history' => $historyRows,
]);
