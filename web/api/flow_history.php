<?php
require_once __DIR__ . '/common.php';

// Simple JSON endpoint to return pump and tank net flow history for the last N seconds.
// Inputs (query params):
//   window_sec (default 21600 = 6h)

$env = require_server_env();
$windowSec = isset($_GET['window_sec']) ? intval($_GET['window_sec']) : 21600;
if ($windowSec <= 0) {
    $windowSec = 21600;
}
$pumpDbPath = resolve_repo_path($env['PUMP_DB_PATH'] ?? '');
$tankDbPath = resolve_repo_path($env['TANK_DB_PATH'] ?? '');

// Determine the latest timestamp across pump + tank streams, use that as the anchor.
$latestTs = null;
function iso_to_ts(?string $iso): ?int {
    if (!$iso) return null;
    $ts = strtotime($iso);
    return $ts === false ? null : $ts;
}

$pumpRows = [];
if ($pumpDbPath && file_exists($pumpDbPath)) {
    $pumpDb = connect_sqlite($pumpDbPath);
    $stmt = $pumpDb->query(
        'SELECT MAX(source_timestamp) AS max_ts FROM pump_events WHERE gallons_per_hour IS NOT NULL'
    );
    if ($row = $stmt->fetch()) {
        $t = iso_to_ts($row['max_ts'] ?? null);
        if ($t && ($latestTs === null || $t > $latestTs)) $latestTs = $t;
    }
}

$netRows = [];
if ($tankDbPath && file_exists($tankDbPath)) {
    $tankDb = connect_sqlite($tankDbPath);
    $stmt = $tankDb->query(
        "SELECT MAX(source_timestamp) AS max_ts FROM tank_readings WHERE flow_gph IS NOT NULL"
    );
    if ($row = $stmt->fetch()) {
        $t = iso_to_ts($row['max_ts'] ?? null);
        if ($t && ($latestTs === null || $t > $latestTs)) $latestTs = $t;
    }
}

$cutoffTs = $latestTs ? $latestTs - $windowSec : (time() - $windowSec);
$cutoffIso = gmdate('c', $cutoffTs);

if ($pumpDbPath && file_exists($pumpDbPath)) {
    $pumpDb = connect_sqlite($pumpDbPath);
    $stmt = $pumpDb->prepare(
        'SELECT source_timestamp AS ts, gallons_per_hour AS flow_gph
         FROM pump_events
         WHERE source_timestamp >= :cutoff AND gallons_per_hour IS NOT NULL
         ORDER BY source_timestamp'
    );
    $stmt->execute([':cutoff' => $cutoffIso]);
    foreach ($stmt as $row) {
        $pumpRows[] = [
            'ts' => $row['ts'],
            'flow_gph' => $row['flow_gph'],
        ];
    }
}

if ($tankDbPath && file_exists($tankDbPath)) {
    $tankDb = connect_sqlite($tankDbPath);
    $stmt = $tankDb->prepare(
        'SELECT tank_id, source_timestamp, flow_gph
         FROM tank_readings
         WHERE source_timestamp >= :cutoff
         ORDER BY source_timestamp'
    );
    $stmt->execute([':cutoff' => $cutoffIso]);
    $byTank = ['brookside' => [], 'roadside' => []];
    foreach ($stmt as $row) {
        $tankId = $row['tank_id'];
        if (!isset($byTank[$tankId])) continue;
        $byTank[$tankId][] = [
            'ts' => $row['source_timestamp'],
            'flow_gph' => $row['flow_gph'],
        ];
    }
    // Build net flow using last-known flows at each event timestamp.
    $events = [];
    foreach ($byTank['brookside'] as $row) {
        $events[] = ['ts' => $row['ts'], 'tank' => 'brookside', 'flow' => $row['flow_gph']];
    }
    foreach ($byTank['roadside'] as $row) {
        $events[] = ['ts' => $row['ts'], 'tank' => 'roadside', 'flow' => $row['flow_gph']];
    }
    usort($events, function ($a, $b) {
        $ta = strtotime($a['ts']);
        $tb = strtotime($b['ts']);
        if ($ta === $tb) return 0;
        return $ta < $tb ? -1 : 1;
    });
    $bFlow = 0.0;
    $rFlow = 0.0;
    foreach ($events as $ev) {
        if ($ev['tank'] === 'brookside') $bFlow = $ev['flow'] ?? 0.0;
        if ($ev['tank'] === 'roadside') $rFlow = $ev['flow'] ?? 0.0;
        $netRows[] = [
            'ts' => $ev['ts'],
            'flow_gph' => $bFlow + $rFlow,
        ];
    }
}

respond_json([
    'status' => 'ok',
    'pump' => $pumpRows,
    'net' => $netRows,
    'window_sec' => $windowSec,
]);
