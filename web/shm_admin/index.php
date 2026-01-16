<?php
require_once __DIR__ . '/../api/common.php';

const ADMIN_REALM = 'SHM Admin';
const USERS_FILE = CONFIG_DIR . '/shm_admin.ini';

function require_basic_auth(): void {
    if (!file_exists(USERS_FILE)) {
        http_response_code(500);
        header('Content-Type: text/plain');
        echo "Missing admin credentials file: " . USERS_FILE . "\n";
        echo "Copy config/example/shm_admin.ini to config/shm_admin.ini and add users.\n";
        exit;
    }
    $users = parse_ini_file(USERS_FILE, false, INI_SCANNER_RAW);
    if (!$users || !is_array($users)) {
        http_response_code(500);
        header('Content-Type: text/plain');
        echo "Invalid admin credentials file: " . USERS_FILE . "\n";
        exit;
    }
    $user = $_SERVER['PHP_AUTH_USER'] ?? '';
    $pass = $_SERVER['PHP_AUTH_PW'] ?? '';
    if (!$user || !$pass) {
        $authHeader = $_SERVER['HTTP_AUTHORIZATION'] ?? ($_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '');
        if ($authHeader && stripos($authHeader, 'basic ') === 0) {
            $decoded = base64_decode(substr($authHeader, 6));
            if ($decoded && strpos($decoded, ':') !== false) {
                [$user, $pass] = explode(':', $decoded, 2);
            }
        }
    }
    $hash = $users[$user] ?? null;
    if (!$user || !$pass || !$hash || !password_verify($pass, $hash)) {
        header('WWW-Authenticate: Basic realm="' . ADMIN_REALM . '"');
        http_response_code(401);
        echo "Unauthorized\n";
        exit;
    }
}

function ensure_display_settings(PDO $db): void {
    $db->exec(
        'CREATE TABLE IF NOT EXISTS display_plot_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            y_axis_min REAL NOT NULL,
            y_axis_max REAL NOT NULL,
            window_sec INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )'
    );
    $stmt = $db->query('SELECT 1 FROM display_plot_settings WHERE id = 1');
    if (!$stmt->fetch()) {
        $now = gmdate('c');
        $db->exec(
            "INSERT INTO display_plot_settings (id, y_axis_min, y_axis_max, window_sec, updated_at)
             VALUES (1, 0.0, 600.0, 7200, '{$now}')"
        );
    }
}

function load_display_settings(PDO $db): array {
    $stmt = $db->query(
        'SELECT y_axis_min, y_axis_max, window_sec, updated_at
         FROM display_plot_settings
         WHERE id = 1'
    );
    if ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
        return [
            'y_axis_min' => (float) $row['y_axis_min'],
            'y_axis_max' => (float) $row['y_axis_max'],
            'window_sec' => (int) $row['window_sec'],
            'updated_at' => $row['updated_at'] ?? null,
        ];
    }
    return [
        'y_axis_min' => 0.0,
        'y_axis_max' => 600.0,
        'window_sec' => 7200,
        'updated_at' => null,
    ];
}

function is_allowed_value($value, array $options): bool {
    foreach ($options as $opt) {
        if ($value == $opt) { // intentional loose compare to accept numeric strings/floats
            return true;
        }
    }
    return false;
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

function render_options(array $options, $selected, array $labels = []): string {
    $out = [];
    foreach ($options as $opt) {
        $value = (string) $opt;
        $label = $labels[$opt] ?? $value;
        $isSelected = ((string) $selected === $value) ? ' selected' : '';
        $out[] = '<option value="' . htmlspecialchars($value, ENT_QUOTES, 'UTF-8') . '"' . $isSelected . '>'
            . htmlspecialchars($label, ENT_QUOTES, 'UTF-8') . '</option>';
    }
    return implode("\n", $out);
}

require_basic_auth();

$env = require_server_env();
$dbPath = resolve_repo_path($env['EVAPORATOR_DB_PATH'] ?? 'data/evaporator.db');
$db = connect_sqlite($dbPath);
ensure_display_settings($db);

$minOptions = [0, 100, 200, 300, 400, 500];
$maxOptions = [300, 400, 500, 600, 700, 800];
$windowOptions = [3600, 7200, 14400, 21600, 28800, 43200];
$windowLabels = [
    3600 => '1 hour',
    7200 => '2 hours',
    14400 => '4 hours',
    21600 => '6 hours',
    28800 => '8 hours',
    43200 => '12 hours',
];

$notice = null;
$error = null;

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'POST') {
    $yMin = isset($_POST['y_axis_min']) ? floatval($_POST['y_axis_min']) : null;
    $yMax = isset($_POST['y_axis_max']) ? floatval($_POST['y_axis_max']) : null;
    $windowSec = isset($_POST['window_sec']) ? intval($_POST['window_sec']) : null;

    if ($yMin === null || $yMax === null || $windowSec === null) {
        $error = 'Missing required fields.';
    } elseif (!is_allowed_value($yMin, $minOptions) || !is_allowed_value($yMax, $maxOptions)) {
        $error = 'Invalid y-axis bounds.';
    } elseif (!is_allowed_value($windowSec, $windowOptions)) {
        $error = 'Invalid time window.';
    } else {
        [$yMin, $yMax] = reconcile_bounds($yMin, $yMax, $minOptions, $maxOptions);
        $stmt = $db->prepare(
            'INSERT INTO display_plot_settings (id, y_axis_min, y_axis_max, window_sec, updated_at)
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
        $notice = 'Display settings updated.';
    }
}

$settings = load_display_settings($db);

header('Content-Type: text/html; charset=utf-8');
?>
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SHM Display Settings</title>
  <style>
    body {
      margin: 0;
      padding: 24px;
      font-family: "Georgia", "Times New Roman", serif;
      background: #f3f3f3;
      color: #1a1a1a;
    }
    .card {
      max-width: 520px;
      margin: 0 auto;
      background: #ffffff;
      padding: 20px 24px;
      border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
    }
    h1 {
      margin: 0 0 12px 0;
      font-size: 22px;
      letter-spacing: 0.5px;
    }
    label {
      display: block;
      margin: 14px 0 6px 0;
      font-weight: 600;
    }
    select {
      width: 100%;
      padding: 6px;
      font-size: 16px;
    }
    .actions {
      margin-top: 18px;
    }
    button {
      padding: 8px 14px;
      font-size: 16px;
      cursor: pointer;
    }
    .notice {
      margin: 12px 0;
      padding: 8px 10px;
      background: #e6f4ea;
      border: 1px solid #b6dfc0;
      color: #1f4f2c;
    }
    .error {
      margin: 12px 0;
      padding: 8px 10px;
      background: #fde8e8;
      border: 1px solid #f3b4b4;
      color: #7a1c1c;
    }
    .meta {
      margin-top: 12px;
      font-size: 13px;
      color: #555;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>Display Pi Settings</h1>
    <?php if ($notice): ?>
      <div class="notice"><?php echo htmlspecialchars($notice, ENT_QUOTES, 'UTF-8'); ?></div>
    <?php endif; ?>
    <?php if ($error): ?>
      <div class="error"><?php echo htmlspecialchars($error, ENT_QUOTES, 'UTF-8'); ?></div>
    <?php endif; ?>
    <form method="post">
      <label for="y-axis-min">Y-axis minimum (gph)</label>
      <select id="y-axis-min" name="y_axis_min">
        <?php echo render_options($minOptions, $settings['y_axis_min']); ?>
      </select>

      <label for="y-axis-max">Y-axis maximum (gph)</label>
      <select id="y-axis-max" name="y_axis_max">
        <?php echo render_options($maxOptions, $settings['y_axis_max']); ?>
      </select>

      <label for="window-sec">History window</label>
      <select id="window-sec" name="window_sec">
        <?php echo render_options($windowOptions, $settings['window_sec'], $windowLabels); ?>
      </select>

      <div class="actions">
        <button type="submit">Save settings</button>
      </div>
    </form>
    <?php if (!empty($settings['updated_at'])): ?>
      <div class="meta">Last updated: <?php echo htmlspecialchars($settings['updated_at'], ENT_QUOTES, 'UTF-8'); ?></div>
    <?php endif; ?>
  </div>
</body>
</html>
