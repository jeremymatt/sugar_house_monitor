#include <Wire.h>
// install the liquidcrystal i2c library by Frank de Brabander
#include <Adafruit_I2CDevice.h>
#include <Adafruit_I2CRegister.h>
#include "Adafruit_MCP9600.h"
#include <LiquidCrystal_I2C.h>
#include <WiFiS3.h>
#include <ArduinoHttpClient.h>
#include <time.h>

#define I2C_ADDRESS (0x67)

// Configure WiFi + API settings here or override via compiler flags.
#ifndef SHM_WIFI_SSID
#define SHM_WIFI_SSID "YOUR_WIFI_SSID"
#endif
#ifndef SHM_WIFI_PASSWORD
#define SHM_WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#endif
#ifndef SHM_API_KEY
#define SHM_API_KEY "REPLACE_WITH_API_KEY"
#endif
#ifndef SHM_API_HOST
#define SHM_API_HOST "mattsmaplesyrup.com"
#endif
#ifndef SHM_API_PATH
#define SHM_API_PATH "/sugar_house_monitor/api/ingest_stacktemp.php"
#endif
#ifndef SHM_SAMPLE_INTERVAL_MS
#define SHM_SAMPLE_INTERVAL_MS 1000UL  // default: 1 sample per second
#endif
#ifndef SHM_USE_TLS
#define SHM_USE_TLS 1  // set to 0 to force HTTP on port 80
#endif
#ifndef SHM_WIFI_RETRY_MS
#define SHM_WIFI_RETRY_MS 500UL
#endif
#ifndef SHM_WIFI_MAX_ATTEMPTS
#define SHM_WIFI_MAX_ATTEMPTS 10
#endif
#ifndef SHM_WINDOW_SIZE_MINUTES
#define SHM_WINDOW_SIZE_MINUTES 15
#endif

const char WIFI_SSID[] = SHM_WIFI_SSID;
const char WIFI_PASSWORD[] = SHM_WIFI_PASSWORD;
const char API_KEY[] = SHM_API_KEY;
const char API_HOST[] = SHM_API_HOST;
const char API_PATH[] = SHM_API_PATH;
const uint16_t API_PORT = SHM_USE_TLS ? 443 : 80;

const unsigned long SAMPLE_INTERVAL_MS = SHM_SAMPLE_INTERVAL_MS;
const unsigned long WIFI_RETRY_DELAY_MS = SHM_WIFI_RETRY_MS;
const int WIFI_ATTEMPTS = SHM_WIFI_MAX_ATTEMPTS;
const int SENSOR_FAILURE_THRESHOLD = 5;
const unsigned long SENSOR_RECOVERY_COOLDOWN_MS = 30000UL;
const int HTTP_FAILURE_THRESHOLD = 3;
const unsigned long WIFI_RECOVERY_COOLDOWN_MS = 30000UL;
const int MCP_REINIT_RETRIES = 3;
const int window_size_minutes = SHM_WINDOW_SIZE_MINUTES;
const int WINDOW_BUFFER_MINUTES = SHM_WINDOW_SIZE_MINUTES * 4;
const unsigned long MINUTE_MS = 60000UL;

const uint8_t LCD_I2C_ADDRESS = 0x27;
const uint8_t LCD_COLUMNS = 20;
const uint8_t LCD_ROWS = 4;

#if SHM_USE_TLS
WiFiSSLClient netClient;
#else
WiFiClient netClient;
#endif

Adafruit_MCP9600 mcp;
LiquidCrystal_I2C lcd(LCD_I2C_ADDRESS, LCD_COLUMNS, LCD_ROWS);
bool lcdReady = false;

/* Set and print ambient resolution */
Ambient_Resolution ambientRes = RES_ZERO_POINT_0625;

unsigned long lastSampleMs = 0;
int sensorFailureCount = 0;
unsigned long lastRecoveryMs = 0;
int httpFailureCount = 0;
unsigned long lastWifiRecoveryMs = 0;
float stackMinuteSums[WINDOW_BUFFER_MINUTES];
uint16_t minuteSampleCounts[WINDOW_BUFFER_MINUTES];
int minuteWriteIndex = 0;
int minutesStored = 0;
float currentMinuteStackSum = 0.0f;
uint16_t currentMinuteCount = 0;
unsigned long currentMinuteStartMs = 0;

inline float C_to_F(float c) {
  return c * 9.0 / 5.0 + 32.0;
}

bool i2cDevicePresent(uint8_t address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}

void initLcd() {
  if (!i2cDevicePresent(LCD_I2C_ADDRESS)) {
    Serial.println("LCD not found; continuing without display");
    lcdReady = false;
    return;
  }
  lcd.init();
  lcd.backlight();
  lcd.clear();
  lcdReady = true;
  Serial.println("LCD initialized");
}

void storeMinuteBucket() {
  stackMinuteSums[minuteWriteIndex] = currentMinuteStackSum;
  minuteSampleCounts[minuteWriteIndex] = currentMinuteCount;
  minuteWriteIndex = (minuteWriteIndex + 1) % WINDOW_BUFFER_MINUTES;
  if (minutesStored < WINDOW_BUFFER_MINUTES) {
    minutesStored += 1;
  }
  currentMinuteStackSum = 0.0f;
  currentMinuteCount = 0;
}

void advanceMinuteBuckets(unsigned long nowMs) {
  if (currentMinuteStartMs == 0) {
    currentMinuteStartMs = nowMs;
    return;
  }
  while (nowMs - currentMinuteStartMs >= MINUTE_MS) {
    storeMinuteBucket();
    currentMinuteStartMs += MINUTE_MS;
  }
}

void recordStackSample(float stackF) {
  currentMinuteStackSum += stackF;
  currentMinuteCount += 1;
}

void getMinuteBucket(int offsetMinutes, float &sum, uint16_t &count) {
  if (offsetMinutes == 0) {
    sum = currentMinuteStackSum;
    count = currentMinuteCount;
    return;
  }
  if (offsetMinutes > minutesStored) {
    sum = 0.0f;
    count = 0;
    return;
  }
  int index = minuteWriteIndex - offsetMinutes;
  if (index < 0) {
    index += WINDOW_BUFFER_MINUTES;
  }
  sum = stackMinuteSums[index];
  count = minuteSampleCounts[index];
}

float windowAverageStackF(int startOffsetMinutes) {
  float sum = 0.0f;
  uint32_t count = 0;
  for (int i = 0; i < window_size_minutes; i++) {
    float bucketSum = 0.0f;
    uint16_t bucketCount = 0;
    getMinuteBucket(startOffsetMinutes + i, bucketSum, bucketCount);
    sum += bucketSum;
    count += bucketCount;
  }
  if (count == 0) {
    return NAN;
  }
  return sum / static_cast<float>(count);
}

int tempForDisplay(float tempF) {
  if (isnan(tempF)) {
    return 0;
  }
  long rounded = (tempF >= 0.0f) ? static_cast<long>(tempF + 0.5f)
                                 : static_cast<long>(tempF - 0.5f);
  if (rounded > 9999) {
    rounded = 9999;
  } else if (rounded < -999) {
    rounded = -999;
  }
  return static_cast<int>(rounded);
}

void lcdPrintRow(uint8_t row, const char *text) {
  if (!lcdReady) {
    return;
  }
  lcd.setCursor(0, row);
  lcd.print(text);
  const int len = strlen(text);
  for (int i = len; i < LCD_COLUMNS; i++) {
    lcd.print(' ');
  }
}

void updateLcd(float stackF, float ambientF,
               float windowA, float windowB, float windowC, float windowD) {
  if (!lcdReady) {
    return;
  }

  char row1[LCD_COLUMNS + 1];
  char row2[LCD_COLUMNS + 1];
  char row3[LCD_COLUMNS + 1];
  char row4[LCD_COLUMNS + 1];

  const int ambientDisplay = tempForDisplay(ambientF);
  const int stackDisplay = tempForDisplay(stackF);
  const int windowADisplay = tempForDisplay(windowA);
  const int windowBDisplay = tempForDisplay(windowB);
  const int windowCDisplay = tempForDisplay(windowC);
  const int windowDDisplay = tempForDisplay(windowD);

  snprintf(row1, sizeof(row1), "Ambient: %04dF", ambientDisplay);
  snprintf(row2, sizeof(row2), "Stack: %04dF", stackDisplay);
  snprintf(row3, sizeof(row3), "%03d:%04dF||%03d:%04dF",
           window_size_minutes, windowADisplay,
           window_size_minutes * 2, windowBDisplay);
  snprintf(row4, sizeof(row4), "%03d:%04dF||%03d:%04dF",
           window_size_minutes * 3, windowCDisplay,
           window_size_minutes * 4, windowDDisplay);

  lcdPrintRow(0, row1);
  lcdPrintRow(1, row2);
  lcdPrintRow(2, row3);
  lcdPrintRow(3, row4);
}

bool hasValidLocalIp(const IPAddress &ip) {
  // Treat 0.0.0.0 as "not really connected" so we wait for DHCP to finish.
  return ip != IPAddress(0, 0, 0, 0);
}

String isoTimestamp() {
  time_t now = WiFi.getTime();
  if (now <= 0) {
    return "";
  }
  tm *tm_info = gmtime(&now);
  if (!tm_info) {
    return "";
  }
  char buf[25];
  size_t written = strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", tm_info);
  if (written == 0) {
    return "";
  }
  return String(buf);
}

bool initMcp(bool verbose) {
  if (!mcp.begin(I2C_ADDRESS)) {
    if (verbose) {
      Serial.println("Sensor not found. Check wiring!");
    }
    return false;
  }

  if (verbose) {
    Serial.println("Found MCP9600!");
  }

  mcp.setAmbientResolution(ambientRes);
  if (verbose) {
    Serial.print("Ambient Resolution set to: ");
    switch (ambientRes) {
      case RES_ZERO_POINT_25:    Serial.println("0.25°C"); break;
      case RES_ZERO_POINT_125:   Serial.println("0.125°C"); break;
      case RES_ZERO_POINT_0625:  Serial.println("0.0625°C"); break;
      case RES_ZERO_POINT_03125: Serial.println("0.03125°C"); break;
    }
  }

  mcp.setADCresolution(MCP9600_ADCRESOLUTION_18);
  if (verbose) {
    Serial.print("ADC resolution set to ");
    switch (mcp.getADCresolution()) {
      case MCP9600_ADCRESOLUTION_18:   Serial.print("18"); break;
      case MCP9600_ADCRESOLUTION_16:   Serial.print("16"); break;
      case MCP9600_ADCRESOLUTION_14:   Serial.print("14"); break;
      case MCP9600_ADCRESOLUTION_12:   Serial.print("12"); break;
    }
    Serial.println(" bits");
  }

  mcp.setThermocoupleType(MCP9600_TYPE_K);
  if (verbose) {
    Serial.print("Thermocouple type set to ");
    switch (mcp.getThermocoupleType()) {
      case MCP9600_TYPE_K:  Serial.print("K"); break;
      case MCP9600_TYPE_J:  Serial.print("J"); break;
      case MCP9600_TYPE_T:  Serial.print("T"); break;
      case MCP9600_TYPE_N:  Serial.print("N"); break;
      case MCP9600_TYPE_S:  Serial.print("S"); break;
      case MCP9600_TYPE_E:  Serial.print("E"); break;
      case MCP9600_TYPE_B:  Serial.print("B"); break;
      case MCP9600_TYPE_R:  Serial.print("R"); break;
    }
    Serial.println(" type");
  }

  mcp.setFilterCoefficient(3);
  if (verbose) {
    Serial.print("Filter coefficient value set to: ");
    Serial.println(mcp.getFilterCoefficient());
  }

  mcp.setAlertTemperature(1, 30);
  if (verbose) {
    Serial.print("Alert #1 temperature set to ");
    Serial.println(mcp.getAlertTemperature(1));
  }
  mcp.configureAlert(1, true, true);  // alert 1 enabled, rising temp

  mcp.enable(true);
  return true;
}

bool recoverMcp() {
  unsigned long now = millis();
  if (now - lastRecoveryMs < SENSOR_RECOVERY_COOLDOWN_MS) {
    return false;
  }
  lastRecoveryMs = now;
  Serial.println("Reinitializing MCP9600...");
  Wire.end();
  delay(5);
  Wire.begin();
  Wire.setClock(100000);  // slow down I2C for stability
#ifdef WIRE_HAS_TIMEOUT
  Wire.setWireTimeout(2500, true);
#endif
  for (int attempt = 1; attempt <= MCP_REINIT_RETRIES; attempt++) {
    if (initMcp(false)) {
      Serial.println("MCP9600 reinit ok");
      return true;
    }
    Serial.print("Reinit attempt ");
    Serial.print(attempt);
    Serial.println(" failed");
    delay(50);
  }
  Serial.println("MCP9600 reinit failed");
  return false;
}

bool connectWifi() {
  Serial.print("Connecting to WiFi SSID ");
  Serial.println(WIFI_SSID);
  WiFi.disconnect();
  WiFi.end();
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  for (int attempt = 0; attempt < WIFI_ATTEMPTS; attempt++) {
    int status = WiFi.status();
    IPAddress ip = WiFi.localIP();
    if (status == WL_CONNECTED && hasValidLocalIp(ip)) {
      Serial.print("WiFi connected, IP: ");
      Serial.println(ip);
      return true;
    }
    if (status == WL_CONNECTED) {
      Serial.print("WiFi connected but waiting for DHCP lease");
    }
    delay(WIFI_RETRY_DELAY_MS);
    Serial.print('.');
  }
  Serial.print("\nWiFi connection failed; status=");
  Serial.println(WiFi.status());
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());
  return false;
}

bool ensureWifi() {
  int status = WiFi.status();
  IPAddress ip = WiFi.localIP();
  if (status == WL_CONNECTED && hasValidLocalIp(ip)) {
    return true;
  }
  if (status == WL_CONNECTED) {
    Serial.println("WiFi connected but missing IP lease; reconnecting...");
  }
  return connectWifi();
}

void resetWifiClient() {
  netClient.stop();
  WiFi.disconnect();
  WiFi.end();
  delay(250);
  connectWifi();
}

bool sendTemps(float stackF, float ambientF) {
  if (!ensureWifi()) {
    Serial.println("WiFi unavailable; skipping upload");
    return false;
  }

  String ts = isoTimestamp();
  String payload = "{\"api_key\":\"";
  payload += API_KEY;
  payload += "\",\"readings\":[{\"stack_temp_f\":";
  payload += String(stackF, 1);
  payload += ",\"ambient_temp_f\":";
  payload += String(ambientF, 1);
  if (ts.length()) {
    payload += ",\"source_timestamp\":\"";
    payload += ts;
    payload += '"';
  }
  payload += "}]}";

  String path = String(API_PATH);
  // Also pass api_key as query param in case headers are stripped by proxies.
  if (path.indexOf("api_key=") < 0) {
    path += path.indexOf('?') >= 0 ? "&" : "?";
    path += "api_key=";
    path += API_KEY;
  }

  Serial.print("POST ");
  Serial.print(API_HOST);
  Serial.print(':');
  Serial.print(API_PORT);
  Serial.println(path);

  HttpClient httpClient(netClient, API_HOST, API_PORT);
  httpClient.beginRequest();
  httpClient.post(path);
  httpClient.sendHeader("Content-Type", "application/json");
  httpClient.sendHeader("X-API-Key", API_KEY);
  httpClient.sendHeader("Content-Length", payload.length());
  httpClient.beginBody();
  httpClient.print(payload);
  httpClient.endRequest();

  int status = httpClient.responseStatusCode();
  String resp = httpClient.responseBody();
  httpClient.stop();

  if (status < 200 || status >= 300) {
    if (status < 0) {
      Serial.println("No HTTP response (connection/TLS issue?)");
    }
    Serial.print("Upload failed (HTTP ");
    Serial.print(status);
    Serial.println(")");
    Serial.println(resp);

    // Attempt WiFi recovery after repeated HTTP/TLS failures.
    httpFailureCount += 1;
    if (status < 0) {
      unsigned long now = millis();
      if (httpFailureCount >= HTTP_FAILURE_THRESHOLD && (now - lastWifiRecoveryMs) >= WIFI_RECOVERY_COOLDOWN_MS) {
        Serial.println("Resetting WiFi after repeated HTTP failures...");
        resetWifiClient();
        lastWifiRecoveryMs = now;
        httpFailureCount = 0;
      }
    }
    return false;
  }

  httpFailureCount = 0;
  Serial.print("Upload ok (HTTP ");
  Serial.print(status);
  Serial.println(")");
  return true;
}

void setup()
{
  Serial.begin(115200);
  while (!Serial) {
    delay(10);
  }
  Serial.println("MCP9600 HW test");

  netClient.setTimeout(15000);
  Wire.begin();
  Wire.setClock(100000);
#ifdef WIRE_HAS_TIMEOUT
  Wire.setWireTimeout(2500, true);
#endif

  /* Initialise the driver with I2C_ADDRESS and the default I2C bus. */
  if (!initMcp(true)) {
    while (1);
  }

  initLcd();

  connectWifi();

  Serial.println(F("------------------------------"));
}


void loop()
{
  const unsigned long now = millis();
  if (now - lastSampleMs < SAMPLE_INTERVAL_MS) {
    return;
  }
  lastSampleMs = now;

  advanceMinuteBuckets(now);

  float hotC  = mcp.readThermocouple();
  float coldC = mcp.readAmbient();

  if (isnan(hotC) || isnan(coldC)) {
    sensorFailureCount += 1;
    Serial.println("Sensor read failed; skipping sample");
    if (sensorFailureCount >= SENSOR_FAILURE_THRESHOLD) {
      if (recoverMcp()) {
        sensorFailureCount = 0;
      }
    }
    return;
  }
  sensorFailureCount = 0;

  const float hotF = C_to_F(hotC);
  const float coldF = C_to_F(coldC);

  recordStackSample(hotF);

  const float windowA = windowAverageStackF(0);
  const float windowB = windowAverageStackF(window_size_minutes);
  const float windowC = windowAverageStackF(window_size_minutes * 2);
  const float windowD = windowAverageStackF(window_size_minutes * 3);

  Serial.print("Stack (hot junction): ");
  Serial.print(hotF, 2);
  Serial.println(" F");

  Serial.print("Ambient (cold junction): ");
  Serial.print(coldF, 2);
  Serial.println(" F");

  Serial.print("ADC: ");
  Serial.print(mcp.readADC() * 2);
  Serial.println(" uV");

  updateLcd(hotF, coldF, windowA, windowB, windowC, windowD);
  sendTemps(hotF, coldF);
}
