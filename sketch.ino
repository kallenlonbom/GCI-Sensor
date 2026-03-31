#include <WiFi.h>
#include <HTTPClient.h>
#include <Arduino.h>

const char* WIFI_SSID     = "Wokwi-GUEST";
const char* WIFI_PASSWORD = "";

const char* SERVER_URL = "https://httpbin.org/post";

#define PIN_AMBIENT_TEMP  34
#define PIN_WATER_TEMP    35
#define PIN_TURBIDITY     32
#define PIN_PH            33

const unsigned long READ_INTERVAL_MS = 5000;  // Send a reading every 5 seconds
unsigned long lastReadTime = 0;

// ESP32 ADC is 12-bit (0–4095) at 3.3V
const float ADC_MAX        = 4095.0;
const float ADC_VOLTAGE    = 3.3;

// rough NTC thermistor to Celsius
float rawToTemperatureC(int raw) {
  float voltage = (raw / ADC_MAX) * ADC_VOLTAGE;
  // Placeholder: assumes linear 0–3.3V maps to -10–100°C
  float tempC = (voltage / ADC_VOLTAGE) * 110.0 - 10.0;
  return tempC;
}

// Turbidity sensor - higher voltage = clearer water
float rawToTurbidityNTU(int raw) {
  float voltage = (raw / ADC_MAX) * ADC_VOLTAGE;
  // approximate placeholder
  float ntu = -1120.4 * (voltage * voltage) + 5742.3 * voltage - 4353.8;
  if (ntu < 0) ntu = 0;
  return ntu;
}

// pH sensor: ~0V = pH 0, ~3.3V = pH 14
float rawToPH(int raw) {
  float voltage = (raw / ADC_MAX) * ADC_VOLTAGE;
  // placeholder
  float ph = (voltage / ADC_VOLTAGE) * 14.0;
  return ph;
}

void connectWiFi() {
  Serial.print("Connecting to WiFi");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi connected!");
    Serial.print("IP Address: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nWiFi connection FAILED. Will retry on next loop.");
  }
}


void postSensorData(float ambientTemp, float waterTemp, float turbidity, float ph) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi not connected — skipping POST");
    return;
  }

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");

  String payload = "{";
  payload += "\"ambient_temp_c\":" + String(ambientTemp, 2) + ",";
  payload += "\"water_temp_c\":"   + String(waterTemp, 2)   + ",";
  payload += "\"turbidity_ntu\":"  + String(turbidity, 2)   + ",";
  payload += "\"ph\":"             + String(ph, 2)          + ",";
  payload += "\"timestamp_ms\":"   + String(millis());
  payload += "}";

  Serial.println("Sending POST: " + payload);

  int httpCode = http.POST(payload);

  if (httpCode > 0) {
    Serial.println("Response code: " + String(httpCode));
    // Serial.println(http.getString());
  } else {
    Serial.println("POST failed, error: " + http.errorToString(httpCode));
  }

  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== ESP32 Sensor Node Starting ===");
  connectWiFi();
}

void loop() {
  // Reconnect if WiFi dropped
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi lost — reconnecting...");
    connectWiFi();
  }

  unsigned long now = millis();
  if (now - lastReadTime >= READ_INTERVAL_MS) {
    lastReadTime = now;

    // Read raw ADC values
    int rawAmbientTemp = analogRead(PIN_AMBIENT_TEMP);
    int rawWaterTemp   = analogRead(PIN_WATER_TEMP);
    int rawTurbidity   = analogRead(PIN_TURBIDITY);
    int rawPH          = analogRead(PIN_PH);

    // Convert to real-world values
    float ambientTemp = rawToTemperatureC(rawAmbientTemp);
    float waterTemp   = rawToTemperatureC(rawWaterTemp);
    float turbidity   = rawToTurbidityNTU(rawTurbidity);
    float ph          = rawToPH(rawPH);

    // Debug print
    Serial.println("--- Sensor Readings ---");
    Serial.printf("  Ambient Temp : %.2f °C  (raw: %d)\n", ambientTemp, rawAmbientTemp);
    Serial.printf("  Water Temp   : %.2f °C  (raw: %d)\n", waterTemp,   rawWaterTemp);
    Serial.printf("  Turbidity    : %.2f NTU (raw: %d)\n", turbidity,   rawTurbidity);
    Serial.printf("  pH           : %.2f     (raw: %d)\n", ph,          rawPH);

    postSensorData(ambientTemp, waterTemp, turbidity, ph);
  }
}
