<?php
require_once __DIR__ . '/common.php';

ensure_post();
$env = require_server_env();
ensure_api_key($env);
$payload = decode_json_body();

$records = $payload['errors'] ?? $payload['error'] ?? $payload;
if (is_array($records) && isset($records['message'])) {
    $records = [$records];
}
if (!is_array($records)) {
    respond_error('Expected an error object or array of errors', 400);
}

$logPath = REPO_ROOT . '/web/error_log.txt';
$dir = dirname($logPath);
if (!is_dir($dir)) {
    mkdir($dir, 0775, true);
}

$accepted = 0;
$now = gmdate('c');
foreach ($records as $record) {
    if (!is_array($record)) {
        continue;
    }
    $timestamp = $record['timestamp'] ?? $now;
    $source = $record['source'] ?? 'pump_pi';
    $message = $record['message'] ?? null;
    if ($message === null || $message === '') {
        continue;
    }
    $line = sprintf("[%s] %s: %s\n", $timestamp, $source, $message);
    file_put_contents($logPath, $line, FILE_APPEND);
    $accepted++;
}

respond_json([
    'status' => 'ok',
    'accepted' => $accepted,
    'written_to' => $logPath,
]);
