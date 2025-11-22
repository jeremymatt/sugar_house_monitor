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
$cutoffIso = gmdate('c', time() - $windowSec);

$pumpDbPath = resolve_repo_path($env['PUMP_DB_PATH'] ?? '');
$tankDbPath = resolve_repo_path($env['TANK_DB_PATH'] ?? '');

$pumpRows = [];
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

$netRows = [];
if ($tankDbPath && file_exists($tankDbPath)) {
    $tankDb = connect_sqlite($tankDbPath);
    // Pull latest readings per tank within window, then pair by closest timestamps.
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
    // Merge by timestamp proximity: assume both streams are roughly aligned; step through in order.
    $i = $j = 0;
    $bs = $byTank['brookside'];
    $rs = $byTank['roadside'];
    while ($i < count($bs) || $j < count($rs)) {
        $b = $i < count($bs) ? $bs[$i] : null;
        $r = $j < count($rs) ? $rs[$j] : null;
        if ($b && $r) {
            // Choose the earlier timestamp as anchor; advance the one that's earlier.
            $bTs = strtotime($b['ts']);
            $rTs = strtotime($r['ts']);
            if ($bTs <= $rTs) {
                $netRows[] = [
                    'ts' => date('c', ($bTs + $rTs) / 2),
                    'flow_gph' => ($b['flow_gph'] ?? 0) + ($r['flow_gph'] ?? 0),
                ];
                $i++; $j++;
            } else {
                $netRows[] = [
                    'ts' => date('c', ($bTs + $rTs) / 2),
                    'flow_gph' => ($b['flow_gph'] ?? 0) + ($r['flow_gph'] ?? 0),
                ];
                $i++; $j++;
            }
        } elseif ($b) {
            $netRows[] = ['ts' => $b['ts'], 'flow_gph' => $b['flow_gph'] ?? 0];
            $i++;
        } elseif ($r) {
            $netRows[] = ['ts' => $r['ts'], 'flow_gph' => $r['flow_gph'] ?? 0];
            $j++;
        }
    }
}

respond_json([
    'status' => 'ok',
    'pump' => $pumpRows,
    'net' => $netRows,
    'window_sec' => $windowSec,
]);
