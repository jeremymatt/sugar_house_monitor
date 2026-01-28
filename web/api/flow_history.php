<?php
require_once __DIR__ . '/common.php';

function table_exists(PDO $db, string $table): bool {
    $stmt = $db->prepare("SELECT name FROM sqlite_master WHERE type='table' AND name = :tbl");
    $stmt->execute([':tbl' => $table]);
    return (bool) $stmt->fetch();
}

// Simple JSON endpoint to return pump, tank net flow, vacuum, and O2 history for the last N seconds.
// Inputs (query params):
//   window_sec (default 21600 = 6h)

$env = require_server_env();
$windowSec = isset($_GET['window_sec']) ? intval($_GET['window_sec']) : 21600;
if ($windowSec <= 0) {
    $windowSec = 21600;
}
$numBins = resolve_num_bins($env, 2000, $_GET['num_bins'] ?? null);
$startRaw = $_GET['start_ts'] ?? null;
$startTs = null;
if ($startRaw) {
    $parsed = strtotime($startRaw);
    if ($parsed !== false) {
        $startTs = $parsed;
    }
}
$pumpDbPath = resolve_repo_path($env['PUMP_DB_PATH'] ?? '');
$tankDbPath = resolve_repo_path($env['TANK_DB_PATH'] ?? '');
$vacuumDbPath = resolve_repo_path($env['VACUUM_DB_PATH'] ?? $env['PUMP_DB_PATH'] ?? $env['TANK_DB_PATH'] ?? '');
$o2DbPath = resolve_repo_path($env['O2_DB_PATH'] ?? '');

// Determine the latest timestamp across pump + tank streams, use that as the anchor.
$latestTs = null;
function iso_to_ts(?string $iso): ?int {
    if (!$iso) return null;
    $ts = strtotime($iso);
    return $ts === false ? null : $ts;
}

if ($startTs === null) {
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

    if ($vacuumDbPath && file_exists($vacuumDbPath)) {
        $vacuumDb = connect_sqlite($vacuumDbPath);
        $check = $vacuumDb->query("SELECT name FROM sqlite_master WHERE type='table' AND name='vacuum_readings'");
        if ($check->fetch()) {
            $stmt = $vacuumDb->query(
                "SELECT MAX(source_timestamp) AS max_ts FROM vacuum_readings WHERE reading_inhg IS NOT NULL"
            );
            if ($row = $stmt->fetch()) {
                $t = iso_to_ts($row['max_ts'] ?? null);
                if ($t && ($latestTs === null || $t > $latestTs)) $latestTs = $t;
            }
        }
    }

    if ($o2DbPath && file_exists($o2DbPath)) {
        $o2Db = connect_sqlite($o2DbPath);
        $check = $o2Db->query("SELECT name FROM sqlite_master WHERE type='table' AND name='o2_readings'");
        if ($check->fetch()) {
            $stmt = $o2Db->query(
                "SELECT MAX(source_timestamp) AS max_ts FROM o2_readings WHERE o2_percent IS NOT NULL"
            );
            if ($row = $stmt->fetch()) {
                $t = iso_to_ts($row['max_ts'] ?? null);
                if ($t && ($latestTs === null || $t > $latestTs)) $latestTs = $t;
            }
        }
    }
}

$startTsUsed = $startTs ?? ($latestTs ? $latestTs - $windowSec : (time() - $windowSec));
$endTsUsed = $startTs ? ($startTs + $windowSec) : ($latestTs ?? time());
$cutoffTs = $startTsUsed;
$cutoffIso = gmdate('c', $cutoffTs);
$endIso = gmdate('c', $endTsUsed);

$pumpBinner = init_series_binner($cutoffTs, $windowSec, $numBins, 'flow_gph');
$netBinner = init_series_binner($cutoffTs, $windowSec, $numBins, 'flow_gph');
$inflowBinner = init_series_binner($cutoffTs, $windowSec, $numBins, 'flow_gph');
$vacuumBinner = init_series_binner($cutoffTs, $windowSec, $numBins, 'reading_inhg');
$o2Binner = init_series_binner($cutoffTs, $windowSec, $numBins, 'o2_percent');

if ($pumpDbPath && file_exists($pumpDbPath)) {
    $pumpDb = connect_sqlite($pumpDbPath);
    $check = $pumpDb->query("SELECT name FROM sqlite_master WHERE type='table' AND name='pump_events'");
    if ($check->fetch()) {
        $stmt = $pumpDb->prepare(
            'SELECT source_timestamp AS ts, gallons_per_hour AS flow_gph
             FROM pump_events
             WHERE source_timestamp >= :cutoff
               AND source_timestamp <= :end
               AND gallons_per_hour IS NOT NULL
             ORDER BY source_timestamp'
        );
        $stmt->execute([':cutoff' => $cutoffIso, ':end' => $endIso]);
        foreach ($stmt as $row) {
            series_binner_add($pumpBinner, [
                'ts' => $row['ts'],
                'flow_gph' => $row['flow_gph'],
            ]);
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
               AND source_timestamp <= :end
             ORDER BY source_timestamp'
        );
        $stmt->execute([':cutoff' => $cutoffIso, ':end' => $endIso]);
        $bFlow = null;
        $rFlow = null;
        foreach ($stmt as $row) {
            $tankId = $row['tank_id'];
            if ($tankId !== 'brookside' && $tankId !== 'roadside') {
                continue;
            }
            $flow = $row['flow_gph'];
            $isOutlier = isset($row['depth_outlier']) && $row['depth_outlier'];
            if ($flow === null || $flow === '' || $isOutlier) {
                continue; // keep last known valid flow
            }
            $flowVal = floatval($flow);
            if ($tankId === 'brookside') {
                $bFlow = $flowVal;
            } else {
                $rFlow = $flowVal;
            }
            if ($bFlow === null && $rFlow === null) {
                continue;
            }
            $net = ($bFlow ?? 0.0) + ($rFlow ?? 0.0);
            $inflow = max($bFlow ?? 0.0, 0.0) + max($rFlow ?? 0.0, 0.0);
            $ts = $row['source_timestamp'];
            series_binner_add($netBinner, [
                'ts' => $ts,
                'flow_gph' => $net,
            ]);
            series_binner_add($inflowBinner, [
                'ts' => $ts,
                'flow_gph' => $inflow,
            ]);
        }
    }
}

if ($vacuumDbPath && file_exists($vacuumDbPath)) {
    $vacuumDb = connect_sqlite($vacuumDbPath);
    $check = $vacuumDb->query("SELECT name FROM sqlite_master WHERE type='table' AND name='vacuum_readings'");
    if ($check->fetch()) {
        $stmt = $vacuumDb->prepare(
            'SELECT source_timestamp AS ts, reading_inhg
             FROM vacuum_readings
             WHERE source_timestamp >= :cutoff
               AND source_timestamp <= :end
               AND reading_inhg IS NOT NULL
             ORDER BY source_timestamp'
        );
        $stmt->execute([':cutoff' => $cutoffIso, ':end' => $endIso]);
        foreach ($stmt as $row) {
            series_binner_add($vacuumBinner, [
                'ts' => $row['ts'],
                'reading_inhg' => $row['reading_inhg'],
            ]);
        }
    }
}

if ($o2DbPath && file_exists($o2DbPath)) {
    $o2Db = connect_sqlite($o2DbPath);
    $check = $o2Db->query("SELECT name FROM sqlite_master WHERE type='table' AND name='o2_readings'");
    if ($check->fetch()) {
        $stmt = $o2Db->prepare(
            'SELECT source_timestamp AS ts, o2_percent
             FROM o2_readings
             WHERE source_timestamp >= :cutoff
               AND source_timestamp <= :end
               AND o2_percent IS NOT NULL
             ORDER BY source_timestamp'
        );
        $stmt->execute([':cutoff' => $cutoffIso, ':end' => $endIso]);
        foreach ($stmt as $row) {
            series_binner_add($o2Binner, [
                'ts' => $row['ts'],
                'o2_percent' => $row['o2_percent'],
            ]);
        }
    }
}

$pumpRows = series_binner_finalize($pumpBinner);
$netRows = series_binner_finalize($netBinner);
$inflowRows = series_binner_finalize($inflowBinner);
$vacuumRows = series_binner_finalize($vacuumBinner);
$o2Rows = series_binner_finalize($o2Binner);

respond_json([
    'status' => 'ok',
    'pump' => $pumpRows,
    'net' => $netRows,
    'inflow' => $inflowRows,
    'vacuum' => $vacuumRows,
    'o2' => $o2Rows,
    'window_sec' => $windowSec,
    'start_ts_used' => $cutoffIso,
    'end_ts_used' => $endIso,
    'num_bins_used' => $numBins,
]);
