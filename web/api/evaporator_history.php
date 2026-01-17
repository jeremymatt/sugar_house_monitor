<?php
require_once __DIR__ . '/common.php';

$env = require_server_env();
$scope = $_GET['scope'] ?? $_POST['scope'] ?? 'web';
if ($scope !== 'display') {
    $scope = 'web';
}
$dbPath = resolve_repo_path($env['EVAPORATOR_DB_PATH'] ?? 'data/evaporator.db');
$stackDbPath = resolve_repo_path(
    $env['STACK_TEMP_DB_PATH']
        ?? $env['VACUUM_DB_PATH']
        ?? $env['PUMP_DB_PATH']
        ?? $env['TANK_DB_PATH']
        ?? ''
);
$numBins = resolve_num_bins($env, 2000, $_GET['num_bins'] ?? null);
$startRaw = $_GET['start_ts'] ?? null;
$startTs = null;
if ($startRaw) {
    $parsed = strtotime($startRaw);
    if ($parsed !== false) {
        $startTs = $parsed;
    }
}
$db = connect_sqlite($dbPath);

$minOptions = [0, 100, 200, 300, 400, 500];
$maxOptions = [300, 400, 500, 600, 700, 800];
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
    $db->exec('CREATE UNIQUE INDEX IF NOT EXISTS idx_evap_sample_timestamp ON evaporator_flow(sample_timestamp)');
    $db->exec(
        'CREATE TABLE IF NOT EXISTS plot_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            y_axis_min REAL NOT NULL,
            y_axis_max REAL NOT NULL,
            window_sec INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )'
    );
    $db->exec(
        'CREATE TABLE IF NOT EXISTS display_plot_settings (
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
             VALUES (1, 0.0, 600.0, 7200, '{$now}')"
        );
    }
    $stmt = $db->query('SELECT 1 FROM display_plot_settings WHERE id = 1');
    if (!$stmt->fetch()) {
        $now = gmdate('c');
        $db->exec(
            "INSERT INTO display_plot_settings (id, y_axis_min, y_axis_max, window_sec, updated_at)
             VALUES (1, 0.0, 600.0, 7200, '{$now}')"
        );
    }
}

function load_settings(PDO $db, string $table): array {
    $stmt = $db->query("SELECT y_axis_min, y_axis_max, window_sec FROM {$table} WHERE id = 1");
    if ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
        return [
            'y_axis_min' => (float) $row['y_axis_min'],
            'y_axis_max' => (float) $row['y_axis_max'],
            'window_sec' => (int) $row['window_sec'],
        ];
    }
    return ['y_axis_min' => 0.0, 'y_axis_max' => 600.0, 'window_sec' => 7200];
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

function is_allowed_value($value, array $options): bool {
    foreach ($options as $opt) {
        if ($value == $opt) { // intentional loose compare to accept numeric strings/floats
            return true;
        }
    }
    return false;
}

ensure_tables($db);
$settingsTable = $scope === 'display' ? 'display_plot_settings' : 'plot_settings';
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

if ($method === 'POST') {
    ensure_post();
    if ($scope === 'display') {
        respond_error('Display settings are managed via shm_admin', 403);
    }
    $payload = decode_json_body();
    $yMin = isset($payload['y_axis_min']) ? floatval($payload['y_axis_min']) : null;
    $yMax = isset($payload['y_axis_max']) ? floatval($payload['y_axis_max']) : null;
    $windowSec = isset($payload['window_sec']) ? intval($payload['window_sec']) : null;

    if ($yMin === null || $yMax === null || $windowSec === null) {
        respond_error('Missing required fields', 400);
    }
    if (!is_allowed_value($yMin, $minOptions) || !is_allowed_value($yMax, $maxOptions)) {
        respond_error('Invalid y-axis bounds', 400);
    }
    if (!is_allowed_value($windowSec, $windowOptions)) {
        respond_error('Invalid window', 400);
    }

    [$yMin, $yMax] = reconcile_bounds($yMin, $yMax, $minOptions, $maxOptions);

    $stmt = $db->prepare(
        "INSERT INTO {$settingsTable} (id, y_axis_min, y_axis_max, window_sec, updated_at)
         VALUES (1, :min, :max, :win, :ts)
         ON CONFLICT(id) DO UPDATE SET
            y_axis_min=excluded.y_axis_min,
            y_axis_max=excluded.y_axis_max,
            window_sec=excluded.window_sec,
            updated_at=excluded.updated_at"
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

$settings = load_settings($db, $settingsTable);
$windowSec = $settings['window_sec'];
if ($scope === 'web') {
    $windowOverride = isset($_GET['window_sec']) ? intval($_GET['window_sec']) : null;
    if ($windowOverride && in_array($windowOverride, $windowOptions, true)) {
        $windowSec = $windowOverride;
    }
}

$latestTs = null;
if ($startTs === null) {
    $stmt = $db->query('SELECT MAX(sample_timestamp) AS max_ts FROM evaporator_flow');
    if ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
        $latestTs = $row['max_ts'] ? strtotime($row['max_ts']) : null;
    }
    if ($stackDbPath && file_exists($stackDbPath)) {
        $stackDb = connect_sqlite($stackDbPath);
        $check = $stackDb->query("SELECT name FROM sqlite_master WHERE type='table' AND name='stack_temperatures'");
        if ($check->fetch()) {
            $stmt = $stackDb->query(
                "SELECT MAX(source_timestamp) AS max_ts FROM stack_temperatures WHERE stack_temp_f IS NOT NULL"
            );
            if ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
                $stackTs = $row['max_ts'] ? strtotime($row['max_ts']) : null;
                if ($stackTs && ($latestTs === null || $stackTs > $latestTs)) {
                    $latestTs = $stackTs;
                }
            }
        }
    }
}
$startTsUsed = $startTs ?? ($latestTs ? $latestTs - $windowSec : (time() - $windowSec));
$endTsUsed = $startTs ? ($startTs + $windowSec) : ($latestTs ?? time());
$cutoffTs = $startTsUsed;
$cutoffIso = gmdate('c', $cutoffTs);
$endIso = gmdate('c', $endTsUsed);

$evapBinner = init_series_binner(
    $cutoffTs,
    $windowSec,
    $numBins,
    'evaporator_flow_gph',
    ['draw_off_tank', 'pump_in_tank']
);
$stackBinner = init_series_binner(
    $cutoffTs,
    $windowSec,
    $numBins,
    'stack_temp_f'
);

$stmt = $db->prepare(
    'SELECT sample_timestamp, evaporator_flow_gph, draw_off_tank, pump_in_tank
     FROM evaporator_flow
     WHERE sample_timestamp >= :cutoff
       AND sample_timestamp <= :end
     ORDER BY sample_timestamp'
);
$stmt->execute([':cutoff' => $cutoffIso, ':end' => $endIso]);
foreach ($stmt as $row) {
    series_binner_add($evapBinner, [
        'ts' => $row['sample_timestamp'],
        'evaporator_flow_gph' => $row['evaporator_flow_gph'],
        'draw_off_tank' => $row['draw_off_tank'],
        'pump_in_tank' => $row['pump_in_tank'],
    ]);
}

if ($stackDbPath && file_exists($stackDbPath)) {
    $stackDb = connect_sqlite($stackDbPath);
    $check = $stackDb->query("SELECT name FROM sqlite_master WHERE type='table' AND name='stack_temperatures'");
    if ($check->fetch()) {
        $stmt = $stackDb->prepare(
            'SELECT source_timestamp, stack_temp_f
             FROM stack_temperatures
             WHERE source_timestamp >= :cutoff
               AND source_timestamp <= :end
               AND stack_temp_f IS NOT NULL
             ORDER BY source_timestamp'
        );
        $stmt->execute([':cutoff' => $cutoffIso, ':end' => $endIso]);
        foreach ($stmt as $row) {
            series_binner_add($stackBinner, [
                'ts' => $row['source_timestamp'],
                'stack_temp_f' => $row['stack_temp_f'],
            ]);
        }
    }
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

$historyRows = series_binner_finalize($evapBinner);
$stackHistoryRows = series_binner_finalize($stackBinner);

respond_json([
    'status' => 'ok',
    'settings' => $settings,
    'window_sec_used' => $windowSec,
    'latest' => $latest,
    'history' => $historyRows,
    'stack_history' => $stackHistoryRows,
    'start_ts_used' => $cutoffIso,
    'end_ts_used' => $endIso,
    'num_bins_used' => $numBins,
]);
