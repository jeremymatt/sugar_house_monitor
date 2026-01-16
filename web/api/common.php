<?php

if (!function_exists('str_contains')) {
    function str_contains(string $haystack, string $needle): bool {
        return $needle === '' || strpos($haystack, $needle) !== false;
    }
}

if (!function_exists('str_starts_with')) {
    function str_starts_with(string $haystack, string $needle): bool {
        return strncmp($haystack, $needle, strlen($needle)) === 0;
    }
}

const REPO_ROOT = __DIR__ . '/../..';
const CONFIG_DIR = REPO_ROOT . '/config';

function respond_json(array $payload, int $status = 200): void {
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($payload);
    exit;
}

function respond_error(string $message, int $status = 400): void {
    respond_json(['status' => 'error', 'message' => $message], $status);
}

function ensure_post(): void {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        respond_error('POST required', 405);
    }
}

function parse_env(string $role): array {
    $path = CONFIG_DIR . '/' . $role . '.env';
    if (!file_exists($path)) {
        respond_error("Config file {$path} not found", 500);
    }
    $entries = [];
    foreach (file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        $trim = trim($line);
        if ($trim === '' || str_starts_with($trim, '#')) {
            continue;
        }
        if (!str_contains($trim, '=')) {
            respond_error("Invalid line in {$path}: {$line}", 500);
        }
        [$key, $value] = explode('=', $trim, 2);
        $entries[trim($key)] = trim($value);
    }
    return $entries;
}

function require_server_env(): array {
    return parse_env('server');
}

function resolve_repo_path(string $path): string {
    if ($path === '') {
        return $path;
    }
    if ($path[0] === '/' || preg_match('#^[A-Za-z]:#', $path)) {
        return $path;
    }
    return REPO_ROOT . '/' . ltrim($path, '/');
}

function parse_positive_int($value, int $default): int {
    if ($value === null) {
        return $default;
    }
    if (is_string($value)) {
        $value = trim($value);
    }
    if ($value === '') {
        return $default;
    }
    if (!is_numeric($value)) {
        return $default;
    }
    $intVal = intval($value);
    return $intVal > 0 ? $intVal : $default;
}

function resolve_num_bins(array $env, int $default, $override = null): int {
    $numBins = parse_positive_int($env['NUM_PLOT_BINS'] ?? null, $default);
    if ($override !== null) {
        $numBins = parse_positive_int($override, $numBins);
    }
    return $numBins;
}

function compute_bin_seconds(int $windowSec, int $numBins): int {
    if ($windowSec <= 0 || $numBins <= 0) {
        return 0;
    }
    return max(1, (int) ceil($windowSec / $numBins));
}

function bin_time_series(
    array $rows,
    string $valueKey,
    int $cutoffTs,
    int $windowSec,
    int $numBins,
    array $carryKeys = []
): array {
    if ($numBins <= 0 || $windowSec <= 0) {
        return $rows;
    }
    if (count($rows) <= $numBins) {
        return $rows;
    }

    $binSec = compute_bin_seconds($windowSec, $numBins);
    if ($binSec <= 0) {
        return $rows;
    }

    $bins = [];
    foreach ($rows as $row) {
        if (!is_array($row)) {
            continue;
        }
        $tsRaw = $row['ts'] ?? null;
        if (!$tsRaw) {
            continue;
        }
        $ts = strtotime($tsRaw);
        if ($ts === false) {
            continue;
        }
        $value = $row[$valueKey] ?? null;
        if ($value === null || $value === '') {
            continue;
        }
        if (!is_numeric($value)) {
            continue;
        }
        $offset = $ts - $cutoffTs;
        if ($offset < 0) {
            continue;
        }
        $idx = (int) floor($offset / $binSec);
        if ($idx < 0) {
            continue;
        }
        if ($idx >= $numBins) {
            $idx = $numBins - 1;
        }
        if (!isset($bins[$idx])) {
            $bins[$idx] = [
                'sum' => 0.0,
                'count' => 0,
                'last_ts' => null,
                'carry' => [],
            ];
        }
        $bins[$idx]['sum'] += floatval($value);
        $bins[$idx]['count'] += 1;
        if ($bins[$idx]['last_ts'] === null || $ts >= $bins[$idx]['last_ts']) {
            $bins[$idx]['last_ts'] = $ts;
            foreach ($carryKeys as $key) {
                if (array_key_exists($key, $row) && $row[$key] !== null) {
                    $bins[$idx]['carry'][$key] = $row[$key];
                }
            }
        }
    }

    ksort($bins);
    $output = [];
    foreach ($bins as $idx => $bin) {
        if (!$bin['count']) {
            continue;
        }
        $centerTs = $cutoffTs + ($idx * $binSec) + ($binSec / 2);
        $entry = [
            'ts' => gmdate('c', (int) round($centerTs)),
            $valueKey => $bin['sum'] / $bin['count'],
        ];
        foreach ($bin['carry'] as $key => $val) {
            $entry[$key] = $val;
        }
        $output[] = $entry;
    }
    return $output;
}

function normalize_int($value): ?int {
    if ($value === null || $value === '') {
        return null;
    }
    if (is_numeric($value)) {
        $intVal = (int) $value;
        return $intVal >= 0 ? $intVal : null;
    }
    return null;
}

function storage_status_path(array $env): string {
    $statusPath = resolve_repo_path($env['STATUS_JSON_PATH'] ?? 'web/data/status.json');
    return rtrim(dirname($statusPath), DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR . 'status_storage.json';
}

function read_json_file(string $path): array {
    if (!file_exists($path)) {
        return [];
    }
    $raw = file_get_contents($path);
    if ($raw === false) {
        return [];
    }
    $data = json_decode($raw, true);
    return is_array($data) ? $data : [];
}

function write_json_file(string $path, array $payload): void {
    $dir = dirname($path);
    if (!is_dir($dir)) {
        mkdir($dir, 0775, true);
    }
    $tmp = tempnam($dir, 'tmp');
    if ($tmp === false) {
        return;
    }
    file_put_contents($tmp, json_encode($payload), LOCK_EX);
    rename($tmp, $path);
}

function apply_storage_entry(array &$storage, string $key, array $payload, string $timestamp): bool {
    $diskTotal = normalize_int($payload['disk_total_bytes'] ?? null);
    $diskUsed = normalize_int($payload['disk_used_bytes'] ?? null);
    $diskFree = normalize_int($payload['disk_free_bytes'] ?? null);
    $diskPath = isset($payload['disk_path']) ? trim((string) $payload['disk_path']) : '';

    if ($diskTotal === null || ($diskUsed === null && $diskFree === null)) {
        return false;
    }
    if ($diskUsed === null && $diskFree !== null) {
        $diskUsed = max(0, $diskTotal - $diskFree);
    }
    if ($diskFree === null && $diskUsed !== null) {
        $diskFree = max(0, $diskTotal - $diskUsed);
    }

    $storage[$key] = [
        'total_bytes' => $diskTotal,
        'used_bytes' => $diskUsed,
        'free_bytes' => $diskFree,
        'path' => $diskPath ?: null,
        'updated_at' => $timestamp,
    ];
    return true;
}

function load_storage_status(array $env): array {
    $path = storage_status_path($env);
    if (!file_exists($path)) {
        $payload = ['generated_at' => gmdate('c')];
        write_json_file($path, $payload);
        return $payload;
    }
    return read_json_file($path);
}

function save_storage_status(array $env, array $storage): void {
    write_json_file(storage_status_path($env), $storage);
}

function init_series_binner(
    int $cutoffTs,
    int $windowSec,
    int $numBins,
    string $valueKey,
    array $carryKeys = []
): array {
    $allowBins = $numBins > 0 && $windowSec > 0;
    return [
        'cutoff_ts' => $cutoffTs,
        'window_sec' => $windowSec,
        'num_bins' => $numBins,
        'bin_sec' => $allowBins ? compute_bin_seconds($windowSec, $numBins) : 0,
        'value_key' => $valueKey,
        'carry_keys' => $carryKeys,
        'allow_bins' => $allowBins,
        'use_bins' => false,
        'raw' => [],
        'bins' => [],
    ];
}

function series_binner_add(array &$state, array $row): void {
    $valueKey = $state['value_key'];
    if (!array_key_exists('ts', $row) || !array_key_exists($valueKey, $row)) {
        return;
    }
    $value = $row[$valueKey];
    if ($value === null || $value === '' || !is_numeric($value)) {
        return;
    }
    if (!$state['allow_bins']) {
        $state['raw'][] = $row;
        return;
    }
    if (!$state['use_bins']) {
        $state['raw'][] = $row;
        if (count($state['raw']) > $state['num_bins']) {
            $state['use_bins'] = true;
            foreach ($state['raw'] as $rawRow) {
                series_binner_add_to_bins($state, $rawRow);
            }
            $state['raw'] = [];
        }
        return;
    }
    series_binner_add_to_bins($state, $row);
}

function series_binner_add_to_bins(array &$state, array $row): void {
    $tsRaw = $row['ts'] ?? null;
    if (!$tsRaw) {
        return;
    }
    $ts = strtotime($tsRaw);
    if ($ts === false) {
        return;
    }
    $offset = $ts - $state['cutoff_ts'];
    if ($offset < 0) {
        return;
    }
    $binSec = $state['bin_sec'];
    if ($binSec <= 0) {
        return;
    }
    $idx = (int) floor($offset / $binSec);
    if ($idx < 0) {
        return;
    }
    if ($idx >= $state['num_bins']) {
        $idx = $state['num_bins'] - 1;
    }
    if (!isset($state['bins'][$idx])) {
        $state['bins'][$idx] = [
            'sum' => 0.0,
            'count' => 0,
            'last_ts' => null,
            'carry' => [],
        ];
    }
    $valueKey = $state['value_key'];
    $state['bins'][$idx]['sum'] += floatval($row[$valueKey]);
    $state['bins'][$idx]['count'] += 1;
    if ($state['bins'][$idx]['last_ts'] === null || $ts >= $state['bins'][$idx]['last_ts']) {
        $state['bins'][$idx]['last_ts'] = $ts;
        foreach ($state['carry_keys'] as $key) {
            if (array_key_exists($key, $row) && $row[$key] !== null) {
                $state['bins'][$idx]['carry'][$key] = $row[$key];
            }
        }
    }
}

function series_binner_finalize(array $state): array {
    if (!$state['use_bins']) {
        return $state['raw'];
    }
    if (!$state['bins']) {
        return [];
    }
    ksort($state['bins']);
    $output = [];
    foreach ($state['bins'] as $idx => $bin) {
        if (!$bin['count']) {
            continue;
        }
        $centerTs = $state['cutoff_ts'] + ($idx * $state['bin_sec']) + ($state['bin_sec'] / 2);
        $entry = [
            'ts' => gmdate('c', (int) round($centerTs)),
            $state['value_key'] => $bin['sum'] / $bin['count'],
        ];
        foreach ($bin['carry'] as $key => $val) {
            $entry[$key] = $val;
        }
        $output[] = $entry;
    }
    return $output;
}

function ensure_api_key(array $env): void {
    $headers = function_exists('getallheaders') ? getallheaders() : [];
    $provided = $headers['X-API-Key']
        ?? $headers['x-api-key']
        ?? ($_POST['api_key'] ?? null)
        ?? ($_GET['api_key'] ?? null);
    if (!$provided || $provided !== ($env['API_KEY'] ?? null)) {
        respond_error('Unauthorized', 401);
    }
}

function decode_json_body(): array {
    $raw = file_get_contents('php://input');
    $data = json_decode($raw, true);
    if ($data === null) {
        respond_error('Invalid JSON body', 400);
    }
    return $data;
}

function connect_sqlite(string $path): PDO {
    $dir = dirname($path);
    if (!is_dir($dir)) {
        mkdir($dir, 0775, true);
    }
    $pdo = new PDO('sqlite:' . $path);
    $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    $pdo->exec('PRAGMA foreign_keys = ON');
    $pdo->exec('PRAGMA journal_mode = WAL');
    $pdo->exec('PRAGMA busy_timeout = 5000');
    return $pdo;
}

function ensure_monitor_table(PDO $db): void {
    $db->exec(
        'CREATE TABLE IF NOT EXISTS monitor_heartbeats (
            stream TEXT PRIMARY KEY,
            last_received_at TEXT NOT NULL
        )'
    );
}

function update_monitor(PDO $db, string $stream, string $timestamp): void {
    ensure_monitor_table($db);
    $stmt = $db->prepare(
        'INSERT INTO monitor_heartbeats (stream, last_received_at)
         VALUES (:stream, :ts)
         ON CONFLICT(stream) DO UPDATE SET last_received_at=excluded.last_received_at'
    );
    $stmt->execute([
        ':stream' => $stream,
        ':ts' => $timestamp,
    ]);
}

function trigger_status_refresh(): void {
    $script = REPO_ROOT . '/scripts/process_status.py';
    $cmd = 'python3 ' . escapeshellarg($script);
    if (strtoupper(substr(PHP_OS, 0, 3)) === 'WIN') {
        exec($cmd, $output, $code);
        if ($code !== 0) {
            error_log('process_status.py failed: ' . implode("\n", $output));
        }
        return;
    }
    // Run async to keep ingest responses fast.
    exec($cmd . ' > /dev/null 2>&1 &');
}
