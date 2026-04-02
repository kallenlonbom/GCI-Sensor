#!/usr/bin/env python3
"""
ESP32 Sensor Proxy Server
Receives raw ADC data from ESP32, applies calibration, optionally forwards.
Type 'start' to begin logging to a named CSV, 'stop' to stop.

First run:  python sensor_proxy.py --setup
Normal run: python sensor_proxy.py
Re-setup:   python sensor_proxy.py --setup   (overwrites existing config)

Requires: pip install flask requests
"""

import csv
import json
import math
import os
import sys
import threading
from datetime import datetime

from flask import Flask, request, jsonify
import requests as req_lib

# ==============================
# FORWARD URL — leave empty to just print calibrated readings, no forwarding
# ==============================
FORWARD_URL = ""

# ==============================
CONFIG_FILE = "sensor_config.json"
LISTEN_PORT = 4999

app = Flask(__name__)
config = {}

ADC_VOLTAGE = 3.3
ADC_MAX = 4095.0

# Board-labeled pin names -> GPIO names used in ESP32 payload
PIN_LABELS = {
    "gpio32":    "D32",
    "gpio34":    "D34",
    "gpio35":    "D35",
    "gpio36_vp": "VP",
    "gpio39_vn": "VN",
}
LABEL_TO_GPIO = {v: k for k, v in PIN_LABELS.items()}

# ==============================
# Logging state — controlled by user input
# ==============================
log_lock = threading.Lock()
log_state = {
    "active": False,
    "file": None,
    "writer": None,
    "filename": None,
}


# ==============================
# Conversion engine
# ==============================
def apply_conversion(value, conv):
    if conv is None or conv.get("type") == "none":
        return value

    t = conv["type"]

    if t == "linear":
        result = conv["slope"] * value + conv["offset"]

    elif t == "polynomial":
        coeffs = conv["coefficients"]
        result = 0
        for i, c in enumerate(coeffs):
            power = len(coeffs) - 1 - i
            result += c * (value ** power)

    elif t == "logarithmic":
        if value <= 0:
            return 0
        result = conv["a"] * math.log(value) + conv["b"]

    else:
        return value

    if "clamp_min" in conv:
        result = max(result, conv["clamp_min"])
    if "clamp_max" in conv:
        result = min(result, conv["clamp_max"])

    return result


def calibrate_reading(pin_name, raw, voltage):
    """Returns (sensor_name, unit, calibrated_value) for a given pin."""
    pin_conf = config.get("pins", {}).get(pin_name)
    if not pin_conf:
        return None, None, None

    conv = pin_conf.get("conversion")
    input_source = pin_conf.get("input", "voltage")

    actual_voltage = voltage
    sensor_max_v = pin_conf.get("sensor_max_voltage")
    adc_ref_v = pin_conf.get("adc_ref_voltage", ADC_VOLTAGE)
    if sensor_max_v and sensor_max_v != adc_ref_v:
        actual_voltage = voltage * (sensor_max_v / adc_ref_v)

    input_val = actual_voltage if input_source == "voltage" else raw

    if conv and conv.get("type") != "none":
        calibrated = apply_conversion(input_val, conv)
    else:
        calibrated = None

    return pin_conf.get("name", pin_name), pin_conf.get("unit", ""), calibrated


# ==============================
# Logging control
# ==============================
def start_logging(filename):
    """Start logging to a new CSV file."""
    with log_lock:
        if log_state["active"]:
            stop_logging_internal()

        if not filename.endswith(".csv"):
            filename += ".csv"

        f = open(filename, "w", newline="")
        writer = csv.writer(f)
        writer.writerow([
            "timestamp",
            "pin", "assigned_name",
            "raw", "voltage", "calibrated", "unit"
        ])
        f.flush()

        log_state["active"] = True
        log_state["file"] = f
        log_state["writer"] = writer
        log_state["filename"] = filename

    print(f"\n>>> LOGGING STARTED -> {filename}")


def stop_logging_internal():
    """Stop logging (must be called with lock held)."""
    if log_state["file"]:
        log_state["file"].close()
    filename = log_state["filename"]
    log_state["active"] = False
    log_state["file"] = None
    log_state["writer"] = None
    log_state["filename"] = None
    return filename


def stop_logging():
    """Stop logging (thread-safe)."""
    with log_lock:
        filename = stop_logging_internal()
    if filename:
        print(f"\n>>> LOGGING STOPPED. Saved to {filename}")
    else:
        print("\n>>> Not currently logging.")


def log_readings(timestamp, entries):
    """Append readings to the active log file (if logging)."""
    with log_lock:
        if not log_state["active"]:
            return
        writer = log_state["writer"]
        for entry in entries:
            writer.writerow([
                timestamp,
                entry["pin"],
                entry["name"],
                entry["raw"],
                entry["voltage"],
                entry.get("calibrated", ""),
                entry.get("unit", ""),
            ])
        log_state["file"].flush()


# ==============================
# Input listener thread
# ==============================
def input_listener():
    """Background thread that listens for start/stop commands."""
    print("\nCommands: 'start' to begin logging, 'stop' to stop logging, 'status' to check\n")

    while True:
        try:
            cmd = input().strip().lower()
        except EOFError:
            break

        if cmd == "start":
            name = input("  Log file name: ").strip()
            if name:
                start_logging(name)
            else:
                print("  No name entered, logging not started.")

        elif cmd == "stop":
            stop_logging()

        elif cmd == "status":
            with log_lock:
                if log_state["active"]:
                    print(f"  Logging to: {log_state['filename']}")
                else:
                    print("  Not logging.")

        elif cmd == "quit" or cmd == "exit":
            stop_logging()
            print("Shutting down...")
            os._exit(0)

        elif cmd:
            print("  Commands: start, stop, status, quit")


# ==============================
# Receive + calibrate + print + log + forward
# ==============================
@app.route("/data", methods=["POST"])
def receive_data():
    data = request.get_json()
    if not data:
        return jsonify({"error": "no JSON received"}), 400

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output = {"timestamp": timestamp}
    log_entries = []

    # --- DHT11 ---
    dht_conf = config.get("dht11", {})
    temp_name = dht_conf.get("temp_name", "dht11_temp")
    hum_name = dht_conf.get("humidity_name", "dht11_humidity")
    temp_unit = dht_conf.get("temp_unit", "C")
    hum_unit = dht_conf.get("humidity_unit", "%")

    dht_temp = data.get("dht11_temp_c")
    dht_hum = data.get("dht11_humidity")
    output[temp_name] = {"value": dht_temp, "unit": temp_unit}
    output[hum_name] = {"value": dht_hum, "unit": hum_unit}

    log_entries.append({
        "pin": "D33",
        "name": temp_name,
        "raw": dht_temp if dht_temp is not None else "",
        "voltage": "",
        "calibrated": dht_temp if dht_temp is not None else "",
        "unit": temp_unit,
    })
    log_entries.append({
        "pin": "D33",
        "name": hum_name,
        "raw": dht_hum if dht_hum is not None else "",
        "voltage": "",
        "calibrated": dht_hum if dht_hum is not None else "",
        "unit": hum_unit,
    })

    # --- ADC pins ---
    adc_data = data.get("adc", {})
    for gpio_name, values in adc_data.items():
        raw = values.get("raw", 0)
        voltage = values.get("voltage", 0.0)
        board_label = PIN_LABELS.get(gpio_name, gpio_name)

        name, unit, calibrated = calibrate_reading(gpio_name, raw, voltage)

        if name:
            entry = {"raw": raw, "voltage": round(voltage, 4)}
            if calibrated is not None:
                entry["calibrated"] = round(calibrated, 4)
                entry["unit"] = unit
            output[name] = entry
        else:
            output[gpio_name] = {"raw": raw, "voltage": round(voltage, 4)}

        log_entries.append({
            "pin": board_label,
            "name": name or board_label,
            "raw": raw,
            "voltage": round(voltage, 4),
            "calibrated": round(calibrated, 4) if calibrated is not None else "",
            "unit": unit or "",
        })

    output["esp32_uptime_ms"] = data.get("timestamp_ms")

    # --- Print ---
    with log_lock:
        logging_indicator = f" [LOG: {log_state['filename']}]" if log_state["active"] else ""
    print(f"\n[{timestamp}]{logging_indicator}")
    if dht_temp is not None:
        print(f"  D33 ({temp_name}): {dht_temp} {temp_unit}")
    else:
        print(f"  D33 ({temp_name}): --")
    if dht_hum is not None:
        print(f"  D33 ({hum_name}): {dht_hum} {hum_unit}")
    else:
        print(f"  D33 ({hum_name}): --")

    for gpio_name in adc_data:
        raw = adc_data[gpio_name].get("raw", 0)
        v = adc_data[gpio_name].get("voltage", 0.0)
        board_label = PIN_LABELS.get(gpio_name, gpio_name)
        name, unit, calibrated = calibrate_reading(gpio_name, raw, v)
        label = name or board_label
        if calibrated is not None:
            print(f"  {board_label} ({label}): {calibrated:.4f} {unit}  (raw: {raw}, {v:.3f}V)")
        else:
            print(f"  {board_label} ({label}): raw {raw}, {v:.3f}V")

    # --- Log to file ---
    log_readings(timestamp, log_entries)

    # --- Forward ---
    if FORWARD_URL:
        try:
            resp = req_lib.post(FORWARD_URL, json=output, timeout=5)
            print(f"  -> Forwarded to {FORWARD_URL} [{resp.status_code}]")
        except Exception as e:
            print(f"  -> Forward FAILED: {e}")

    return jsonify({"status": "ok"}), 200


# ==============================
# Interactive setup wizard
# ==============================
def run_setup():
    print("=" * 50)
    print("  ESP32 Sensor Proxy — Configuration Setup")
    print("=" * 50)

    conf = {"pins": {}, "dht11": {}}

    # --- DHT11 ---
    print("\n--- D33: DHT11 / DHT22 (temp + humidity) ---")
    print("  This is the digital temp/humidity sensor on pin D33.")
    print("  Values come pre-calibrated from the sensor, just naming them here.\n")
    temp_name = input("  Name for temperature reading [ambient_temp]: ").strip() or "ambient_temp"
    temp_unit = input("  Unit for temperature [C]: ").strip() or "C"
    hum_name = input("  Name for humidity reading [ambient_humidity]: ").strip() or "ambient_humidity"
    hum_unit = input("  Unit for humidity [%]: ").strip() or "%"
    conf["dht11"] = {
        "temp_name": temp_name,
        "humidity_name": hum_name,
        "temp_unit": temp_unit,
        "humidity_unit": hum_unit,
    }

    # --- ADC pins ---
    pin_order = ["D32", "D34", "D35", "VP", "VN"]
    print("\n--- Analog Pins ---")
    print("  5 analog input pins available: D32, D34, D35, VP, VN")
    print("  For each pin, give it a sensor name and optional conversion.")
    print("  Leave name blank to skip — raw data still gets sent.\n")

    for board_label in pin_order:
        gpio_name = LABEL_TO_GPIO[board_label]
        print(f"  [{board_label}]")
        name = input(f"    Sensor name (blank to skip): ").strip()
        if not name:
            print()
            continue

        unit = input(f"    Unit (e.g. NTU, pH, cm, mV): ").strip()

        # Voltage scaling
        print(f"    Voltage setup:")
        print(f"      Your ESP32 ADC reads 0-3.3V.")
        print(f"      If this sensor is designed for a higher voltage (e.g. 5V),")
        print(f"      enter the sensor's max output voltage to scale readings.")
        sensor_max_v_str = input(f"    Sensor max voltage [3.3 = no scaling]: ").strip()
        sensor_max_v = float(sensor_max_v_str) if sensor_max_v_str else 3.3

        # Input source
        input_source = ""
        while input_source not in ("raw", "voltage"):
            input_source = input(f"    Apply conversion to [raw] or [voltage]? [voltage]: ").strip().lower()
            if not input_source:
                input_source = "voltage"

        # Conversion type
        print(f"    Conversion types:")
        print(f"      1. none        — no conversion, just pass through raw/voltage")
        print(f"      2. linear      — y = slope * x + offset")
        print(f"      3. polynomial  — y = c0*x^n + c1*x^(n-1) + ... + cn")
        print(f"      4. logarithmic — y = a * ln(x) + b")

        choice = input(f"    Select [1-4]: ").strip()

        conv = {"type": "none"}

        if choice == "2":
            slope = float(input("    slope: "))
            offset = float(input("    offset: "))
            conv = {"type": "linear", "slope": slope, "offset": offset}

        elif choice == "3":
            coeff_str = input("    coefficients (high-order first, comma-separated): ")
            coeffs = [float(c.strip()) for c in coeff_str.split(",")]
            conv = {"type": "polynomial", "coefficients": coeffs}

        elif choice == "4":
            a = float(input("    a: "))
            b = float(input("    b: "))
            conv = {"type": "logarithmic", "a": a, "b": b}

        # Clamp
        do_clamp = input("    Set output bounds? (e.g. pH 0-14) [y/N]: ").strip().lower()
        if do_clamp == "y":
            cmin = input("      min (blank for none): ").strip()
            cmax = input("      max (blank for none): ").strip()
            if cmin:
                conv["clamp_min"] = float(cmin)
            if cmax:
                conv["clamp_max"] = float(cmax)

        pin_conf = {
            "name": name,
            "unit": unit,
            "input": input_source,
            "conversion": conv,
        }
        if sensor_max_v != 3.3:
            pin_conf["sensor_max_voltage"] = sensor_max_v
            pin_conf["adc_ref_voltage"] = 3.3

        conf["pins"][gpio_name] = pin_conf
        print()

    # --- Save ---
    with open(CONFIG_FILE, "w") as f:
        json.dump(conf, f, indent=2)

    print(f"\nConfig saved to {CONFIG_FILE}")
    print(f"You can also edit {CONFIG_FILE} directly to tweak values.")
    print(f"Run without --setup to start the server.\n")


# ==============================
# Main
# ==============================
if __name__ == "__main__":
    if "--setup" in sys.argv:
        run_setup()
    else:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                config = json.load(f)
            print(f"Loaded config from {CONFIG_FILE}")

            dht = config.get("dht11", {})
            pins = config.get("pins", {})
            print(f"  D33 -> {dht.get('temp_name', '?')} ({dht.get('temp_unit', '?')}), "
                  f"{dht.get('humidity_name', '?')} ({dht.get('humidity_unit', '?')})")
            for gpio_name, pc in pins.items():
                board_label = PIN_LABELS.get(gpio_name, gpio_name)
                conv_type = pc.get("conversion", {}).get("type", "none")
                scale_note = ""
                if pc.get("sensor_max_voltage"):
                    scale_note = f", scaled {pc['adc_ref_voltage']}V->{pc['sensor_max_voltage']}V"
                print(f"  {board_label} -> {pc['name']} ({pc.get('unit', '?')}) [{conv_type}{scale_note}]")
            all_pins = ["gpio32", "gpio34", "gpio35", "gpio36_vp", "gpio39_vn"]
            unconfigured = [PIN_LABELS.get(p, p) for p in all_pins if p not in pins]
            if unconfigured:
                print(f"  Unconfigured (raw only): {', '.join(unconfigured)}")
        else:
            print(f"No {CONFIG_FILE} found — run with --setup first, or all readings will be raw.")
            config = {"pins": {}, "dht11": {}}

        print(f"\nListening on port {LISTEN_PORT}")
        if FORWARD_URL:
            print(f"Forwarding calibrated data to {FORWARD_URL}")
        else:
            print("No FORWARD_URL set — print only, no forwarding.")
        print("\nNot logging. Type 'start' to begin logging to a CSV file.")
        print("Readings will print to console regardless.\n")

        # Start input listener in background thread
        input_thread = threading.Thread(target=input_listener, daemon=True)
        input_thread.start()

        app.run(host="0.0.0.0", port=LISTEN_PORT)
