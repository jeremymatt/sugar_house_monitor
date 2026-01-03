<?php
require_once __DIR__ . '/common.php';

function table_exists(PDO $db, string $table): bool {
    $stmt = $db->prepare("SELECT name FROM sqlite_master WHERE type='table' AND name = :tbl");
    $stmt->execute([':tbl' => $table]);
    return (bool) $stmt->fetch();
}

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
    $check = $pumpDb->query("SELECT name FROM sqlite_master WHERE type='table' AND name='pump_events'");
    if ($check->fetch()) {
        $stmt = $pumpDb->query(
            'SELECT MAX(source_timestamp) AS max_ts FROM pump_events WHERE gallons_per_hour IS NOT NULL'
        );
        if ($row = $stmt->fetch()) {
            $t = iso_to_ts($row['max_ts'] ?? null);
            if ($t && ($latestTs === null || $t > $latestTs)) $latestTs = $t;
        }
    }
}

$netRows = [];
$inflowRows = [];
if ($tankDbPath && file_exists($tankDbPath)) {
    $tankDb = connect_sqlite($tankDbPath);
    $check = $tankDb->query("SELECT name FROM sqlite_master WHERE type='table' AND name='tank_readings'");
    if ($check->fetch()) {
        $stmt = $tankDb->query(
            "SELECT MAX(source_timestamp) AS max_ts FROM tank_readings WHERE flow_gph IS NOT NULL"
        );
        if ($row = $stmt->fetch()) {
            $t = iso_to_ts($row['max_ts'] ?? null);
            if ($t && ($latestTs === null || $t > $latestTs)) $latestTs = $t;
        }
    }
}

$cutoffTs = $latestTs ? $latestTs - $windowSec : (time() - $windowSec);
$cutoffIso = gmdate('c', $cutoffTs);

if ($pumpDbPath && file_exists($pumpDbPath)) {
    $pumpDb = connect_sqlite($pumpDbPath);
    $check = $pumpDb->query("SELECT name FROM sqlite_master WHERE type='table' AND name='pump_events'");
    if ($check->fetch()) {
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
}

if ($tankDbPath && file_exists($tankDbPath)) {
    $tankDb = connect_sqlite($tankDbPath);
    $check = $tankDb->query("SELECT name FROM sqlite_master WHERE type='table' AND name='tank_readings'");
    if ($check->fetch()) {
        $stmt = $tankDb->prepare(
            'SELECT tank_id, source_timestamp, flow_gph, depth_outlier
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
                'depth_outlier' => $row['depth_outlier'] ?? null,
            ];
        }
        // Build net flow using last-known flows at each event timestamp.
        $events = [];
        foreach ($byTank['brookside'] as $row) {
            $events[] = ['ts' => $row['ts'], 'tank' => 'brookside', 'flow' => $row['flow_gph'], 'outlier' => $row['depth_outlier']];
        }
        foreach ($byTank['roadside'] as $row) {
            $events[] = ['ts' => $row['ts'], 'tank' => 'roadside', 'flow' => $row['flow_gph'], 'outlier' => $row['depth_outlier']];
        }
        usort($events, function ($a, $b) {
            $ta = strtotime($a['ts']);
            $tb = strtotime($b['ts']);
            if ($ta === $tb) return 0;
            return $ta < $tb ? -1 : 1;
        });
        $bFlow = null;
        $rFlow = null;
        foreach ($events as $ev) {
            $flow = $ev['flow'];
            $isOutlier = isset($ev['outlier']) && $ev['outlier'];
            if ($flow === null || $flow === '' || $isOutlier) {
                continue; // keep last known valid flow
            }
            $flowVal = floatval($flow);
            if ($ev['tank'] === 'brookside') $bFlow = $flowVal;
            if ($ev['tank'] === 'roadside') $rFlow = $flowVal;
            if ($bFlow === null && $rFlow === null) {
                continue;
            }
            $net = ($bFlow ?? 0.0) + ($rFlow ?? 0.0);
            $inflow = max($bFlow ?? 0.0, 0.0) + max($rFlow ?? 0.0, 0.0);
            $netRows[] = [
                'ts' => $ev['ts'],
                'flow_gph' => $net,
            ];
            $inflowRows[] = [
                'ts' => $ev['ts'],
                'flow_gph' => $inflow,
            ];
        }
    }
}

respond_json([
    'status' => 'ok',
    'pump' => $pumpRows,
    'net' => $netRows,
    'inflow' => $inflowRows,
    'window_sec' => $windowSec,
]);
