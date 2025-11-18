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
    $pdo->exec('PRAGMA busy_timeout = 5000');
    return $pdo;
}

function trigger_status_refresh(): void {
    $cmd = escapeshellcmd('python3 ' . REPO_ROOT . '/scripts/process_status.py');
    exec($cmd, $output, $code);
    if ($code !== 0) {
        error_log('process_status.py failed: ' . implode("\n", $output));
    }
}
