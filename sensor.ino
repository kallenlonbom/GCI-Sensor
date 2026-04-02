#include <WiFi.h>
#include <HTTPClient.h>
#include <Arduino.h>
#include <DHT.h>
#include <WiFiManager.h>
#include <Preferences.h>

// ==============================
// Pin assignments
// ==============================
#define PIN_DHT11   33   // digital — DHT11 temp/humidity

// All ADC1 pins — read everything, send raw, sort it out server-side
const int ADC_PINS[]   = {32, 34, 35, 36, 39};
const char* ADC_NAMES[] = {"gpio32", "gpio34", "gpio35", "gpio36_vp", "gpio39_vn"};
const int NUM_ADC_PINS = 5;

// ==============================
// DHT11
// ==============================
DHT dht(PIN_DHT11, DHT11);

// ==============================
// Config stored in flash (survives reboot)
// ==============================
Preferences prefs;
char serverUrl[128] = "http://192.168.1.154:4999/data";

// WiFiManager custom parameter for server URL
WiFiManagerParameter customServer("server", "Server URL", serverUrl, 128);

const unsigned long READ_INTERVAL_MS = 10000;
unsigned long lastReadTime = 0;

const float ADC_MAX     = 4095.0;
const float ADC_VOLTAGE = 3.3;

// ==============================
// Save server URL to flash when set via WiFiManager
// ==============================
void saveConfigCallback() {
  strncpy(serverUrl, customServer.getValue(), sizeof(serverUrl) - 1);
  prefs.begin("config", false);
  prefs.putString("serverUrl", serverUrl);
  prefs.end();
  Serial.println("Saved server URL: " + String(serverUrl));
}

// ==============================
// WiFi setup via WiFiManager
// ==============================
void setupWiFi() {
  WiFiManager wm;

  wm.resetSettings();

  // Load saved server URL from flash
  prefs.begin("config", true);
  String savedUrl = prefs.getString("serverUrl", serverUrl);
  prefs.end();
  strncpy(serverUrl, savedUrl.c_str(), sizeof(serverUrl) - 1);
  customServer.setValue(serverUrl, 128);

  wm.addParameter(&customServer);
  wm.setSaveParamsCallback(saveConfigCallback);

  // If it can't connect, it starts an AP called "ESP32-Sensor"
  // Connect to that with your phone, configure WiFi + server URL
  wm.setConfigPortalTimeout(120);  // portal stays open 2 min then retries

  Serial.println("Starting WiFiManager...");
  if (!wm.autoConnect("ESP32-Sensor")) {
    Serial.println("WiFiManager failed to connect. Restarting...");
    ESP.restart();
  }

  // Save server URL in case it was changed
  saveConfigCallback();

  Serial.println("WiFi connected!");
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());
  Serial.print("Server: ");
  Serial.println(serverUrl);
}

// ==============================
// POST — all raw ADC values + DHT11
// ==============================
void postSensorData(float dhtTemp, float dhtHumidity, int adcValues[]) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi not connected — skipping POST");
    return;
  }

  HTTPClient http;
  http.begin(serverUrl);
  http.addHeader("Content-Type", "application/json");

  String payload = "{";

  // DHT11
  if (!isnan(dhtTemp)) {
    payload += "\"dht11_temp_c\":" + String(dhtTemp, 2) + ",";
    payload += "\"dht11_humidity\":" + String(dhtHumidity, 2) + ",";
  } else {
    payload += "\"dht11_temp_c\":null,";
    payload += "\"dht11_humidity\":null,";
  }

  // All ADC pins — raw value + voltage
  payload += "\"adc\":{";
  for (int i = 0; i < NUM_ADC_PINS; i++) {
    float voltage = (adcValues[i] / ADC_MAX) * ADC_VOLTAGE;
    payload += "\"" + String(ADC_NAMES[i]) + "\":{";
    payload += "\"raw\":" + String(adcValues[i]) + ",";
    payload += "\"voltage\":" + String(voltage, 4);
    payload += "}";
    if (i < NUM_ADC_PINS - 1) payload += ",";
  }
  payload += "},";

  payload += "\"timestamp_ms\":" + String(millis());
  payload += "}";

  Serial.println("POST: " + payload);

  int httpCode = http.POST(payload);
  if (httpCode > 0) {
    Serial.println("Response: " + String(httpCode));
  } else {
    Serial.println("POST failed: " + http.errorToString(httpCode));
  }

  http.end();
}

// ==============================
// Setup
// ==============================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== ESP32 Sensor Node Starting ===");

  dht.begin();
  Serial.println("Waiting for DHT11 to stabilize...");
  delay(3000);

  setupWiFi();
}

// ==============================
// Loop
// ==============================
void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi lost — restarting to trigger WiFiManager...");
    ESP.restart();
  }

  unsigned long now = millis();
  if (now - lastReadTime >= READ_INTERVAL_MS) {
    lastReadTime = now;

    // DHT11
    float dhtTemp     = dht.readTemperature();
    float dhtHumidity = dht.readHumidity();

    // Read all ADC1 pins
    int adcValues[NUM_ADC_PINS];
    for (int i = 0; i < NUM_ADC_PINS; i++) {
      adcValues[i] = analogRead(ADC_PINS[i]);
    }

    // Serial output
    Serial.println("--- Sensor Readings ---");
    if (isnan(dhtTemp) || isnan(dhtHumidity)) {
      Serial.println("  DHT11: read FAILED");
    } else {
      Serial.printf("  DHT11 Temp : %.1f C\n", dhtTemp);
      Serial.printf("  DHT11 Hum  : %.1f %%\n", dhtHumidity);
    }
    for (int i = 0; i < NUM_ADC_PINS; i++) {
      float v = (adcValues[i] / ADC_MAX) * ADC_VOLTAGE;
      Serial.printf("  %-10s : raw %4d, %.3fV\n", ADC_NAMES[i], adcValues[i], v);
    }

    postSensorData(dhtTemp, dhtHumidity, adcValues);
  }
}
