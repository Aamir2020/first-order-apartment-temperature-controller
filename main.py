
import os
import time
import json
import network
import dht
import requests
import machine

from machine import Pin, PWM
from thermal import ThermalModel, HeatingController

# ============================================================
# Configuration
# ============================================================

# Hardware pins
DHT_PIN = 18
SERVO_PIN = 19

# Wi-Fi name and password live in secrets.py (not in git).
from secrets import WIFI_SSID, WIFI_PASSWORD

# HTTP server
WEB_SERVER = "192.168.1.67"
WEB_PORT = 8766

WEB_PATH_GET = "/api/target?esp"
WEB_PATH_POST = "/api/device/report"

BASE_URL = "http://{}:{}".format(WEB_SERVER, WEB_PORT)

# Outdoor temperature from Open-Meteo (no API key needed).
# Coordinates are Edmonton.
OUTDOOR_LATITUDE = 53.55
OUTDOOR_LONGITUDE = -113.49
OUTDOOR_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={}&longitude={}&current=temperature_2m".format(
        OUTDOOR_LATITUDE, OUTDOOR_LONGITUDE
    )
)
OUTDOOR_REFRESH_SECONDS = 15 * 60
# Forget the outdoor temperature if it cannot be refreshed for this
# long; the controller then falls back to simple on/off.
OUTDOOR_MAX_AGE_SECONDS = 2 * 60 * 60

# Heater power, only used to print R and C in physical units.
HEATER_POWER_WATTS = 1500

# Files on the ESP32 flash.
MODEL_FILE = "model.json"
LOG_FILE = "log.txt"
LOG_MAX_BYTES = 16 * 1024

# Timing
POLL_INTERVAL_SECONDS = 2

# Servo configuration
SERVO_FREQUENCY = 50

# Typical hobby servo pulse range; calibrate for your servo.
SERVO_MIN_US = 500
SERVO_MAX_US = 2500

# Requested positions:
# 0% = heating OFF
# 100% = heating ON
SERVO_OFF_PERCENT = 0
SERVO_ON_PERCENT = 100


# ============================================================
# Logging
# ============================================================

def log(*args):
    """
    Print, and also append to LOG_FILE so the log can be read
    over Wi-Fi (./ota_get.sh log.txt). Only used for model and
    heater events, not every 2-second reading, to limit flash wear.
    """
    line = " ".join(str(a) for a in args)
    print(line)

    try:
        try:
            if os.stat(LOG_FILE)[6] > LOG_MAX_BYTES:
                os.rename(LOG_FILE, LOG_FILE + ".old")
        except OSError:
            pass

        with open(LOG_FILE, "a") as f:
            t = time.gmtime()
            f.write("{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}Z {}\n".format(
                t[0], t[1], t[2], t[3], t[4], t[5], line
            ))
    except OSError:
        pass


# ============================================================
# Wi-Fi
# ============================================================

def connect_wifi():
    wlan = network.WLAN(network.WLAN.IF_STA)
    wlan.active(True)

    if wlan.isconnected():
        print("Wi-Fi already connected:", wlan.ifconfig())
        return wlan

    print("Connecting to Wi-Fi...")

    for attempt in range(10):
        try:
            wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        except OSError:
            pass

        for _ in range(5):
            if wlan.isconnected():
                print("Wi-Fi connected:", wlan.ifconfig())
                return wlan
            time.sleep(1)

        print("Wi-Fi connection attempt", attempt + 1, "failed")

    raise RuntimeError("Could not connect to Wi-Fi")


# ============================================================
# Hardware initialization
# ============================================================

class DHT11WithDecimals(dht.DHT11):
    """
    This sensor sends DHT11-format data (whole number in one byte)
    but also fills in the decimal byte, which MicroPython's DHT11
    class discards. Including it gives 0.1 C resolution.
    """

    def humidity(self):
        return self.buf[0] + self.buf[1] * 0.1

    def temperature(self):
        t = self.buf[2] + (self.buf[3] & 0x7F) * 0.1
        if self.buf[3] & 0x80:
            t = -t
        return t


sensor = DHT11WithDecimals(Pin(DHT_PIN))

# Created with no pulses (duty 0) so the servo stays still until
# main() sets the first position. Passing duty_ns to the constructor
# raises "PWM is inactive" on MicroPython 1.29.
servo = PWM(Pin(SERVO_PIN), freq=SERVO_FREQUENCY)

# How long to keep sending pulses after a move before going quiet.
SERVO_SETTLE_SECONDS = 1

_servo_percent = None


def set_servo_position(percent):
    """
    Set servo position from 0 to 100 percent.

    This maps position percentage to a pulse width between
    SERVO_MIN_US and SERVO_MAX_US. It does not set literal
    PWM duty cycle to that percentage.

    Pulses are only sent while the servo moves, then stopped, so it
    does not buzz or draw current holding its position.
    """
    global _servo_percent

    percent = max(0, min(100, percent))

    if percent == _servo_percent:
        return

    pulse_us = SERVO_MIN_US + (
        (SERVO_MAX_US - SERVO_MIN_US) * percent // 100
    )

    servo.duty_ns(pulse_us * 1000)
    time.sleep(SERVO_SETTLE_SECONDS)
    servo.duty_ns(0)

    _servo_percent = percent


# ============================================================
# HTTP API
# ============================================================

def get_target_temperature():
    url = BASE_URL + WEB_PATH_GET
    response = None

    try:
        response = requests.get(url)

        if response.status_code != 200:
            raise RuntimeError(
                "Target API returned HTTP {}".format(
                    response.status_code
                )
            )

        data = response.json()
        target = float(data["target"])

        print("Target temperature:", target)
        return target

    finally:
        if response is not None:
            response.close()


def report_measurements(temperature, humidity, heating):
    url = BASE_URL + WEB_PATH_POST

    payload = {
        "temp": round(temperature, 1),
        "humidity": round(humidity),
        "heating": heating
    }

    response = None

    try:
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload)
        )

        print("Report HTTP status:", response.status_code)

        if response.status_code < 200 or response.status_code >= 300:
            print("Warning: measurement report failed")

    finally:
        if response is not None:
            response.close()


def get_outdoor_temperature():
    response = None

    try:
        response = requests.get(OUTDOOR_URL)

        if response.status_code != 200:
            raise RuntimeError(
                "Weather API returned HTTP {}".format(
                    response.status_code
                )
            )

        return float(response.json()["current"]["temperature_2m"])

    finally:
        if response is not None:
            response.close()


# ============================================================
# Model persistence
# ============================================================

def load_model():
    model = ThermalModel()

    try:
        with open(MODEL_FILE) as f:
            model.load_dict(json.load(f))
        log("Loaded thermal model:", model.to_dict())
    except (OSError, ValueError, KeyError):
        log("No saved thermal model; starting from defaults")

    return model


def save_model(model):
    # Write to a temporary file and rename, so a reset mid-write
    # cannot corrupt the learned model.
    try:
        with open(MODEL_FILE + ".tmp", "w") as f:
            json.dump(model.to_dict(), f)
        try:
            os.remove(MODEL_FILE)
        except OSError:
            pass
        os.rename(MODEL_FILE + ".tmp", MODEL_FILE)
    except OSError as exc:
        log("Could not save model:", exc)


# ============================================================
# Main control loop
# ============================================================

def main():
    # Set the clock so log timestamps are real (UTC) times.
    try:
        import ntptime
        ntptime.settime()
    except Exception as exc:
        print("NTP error:", exc)

    log("Starting ESP32 temperature controller")

    wlan = network.WLAN(network.WLAN.IF_STA)

    # Initialize servo to heating OFF.
    set_servo_position(SERVO_OFF_PERCENT)

    model = load_model()
    controller = HeatingController(model, log=log)
    saved_samples = model.samples

    # Log the raw sensor bytes once, to confirm whether the sensor
    # speaks the DHT11 or DHT22 data format.
    try:
        sensor.measure()
        log("DHT raw bytes:", list(sensor.buf))
    except Exception as exc:
        log("DHT raw read error:", exc)

    target = None
    tout = None
    tout_age = None
    next_outdoor_fetch = 0
    heater = False

    # Monotonic seconds built from ticks_diff, because ticks_ms
    # wraps after about 12 days.
    now = 0.0
    last_ticks = time.ticks_ms()

    while True:
        ticks = time.ticks_ms()
        elapsed = time.ticks_diff(ticks, last_ticks) / 1000
        last_ticks = ticks
        now += elapsed

        if tout_age is not None:
            tout_age += elapsed
            if tout_age > OUTDOOR_MAX_AGE_SECONDS:
                tout = None

        temperature = None

        try:
            # Ensure Wi-Fi is connected.
            if not wlan.isconnected():
                print("Wi-Fi disconnected; reconnecting...")
                wlan.connect(WIFI_SSID, WIFI_PASSWORD)

                for _ in range(10):
                    if wlan.isconnected():
                        break
                    time.sleep(1)

            # Read the sensor.
            try:
                sensor.measure()
                temperature = sensor.temperature()
                humidity = sensor.humidity()

                print(
                    "Temperature: {:.1f} C | Humidity: {:.1f}%".format(
                        temperature, humidity
                    )
                )
            except OSError as exc:
                print("Sensor error:", exc)

            if wlan.isconnected():
                # Report readings first, so a failing target request
                # does not stop measurements from reaching the server.
                if temperature is not None:
                    try:
                        report_measurements(temperature, humidity, heater)
                    except Exception as exc:
                        print("Report error:", exc)

                try:
                    target = get_target_temperature()
                except Exception as exc:
                    print("Target error:", exc)

                if now >= next_outdoor_fetch:
                    try:
                        tout = get_outdoor_temperature()
                        tout_age = 0
                        next_outdoor_fetch = now + OUTDOOR_REFRESH_SECONDS
                        log("Outdoor temperature:", tout)
                    except Exception as exc:
                        log("Outdoor temperature error:", exc)
                        # Retry in a minute rather than every loop.
                        next_outdoor_fetch = now + 60

            if target is None:
                # Never heat without a target from the server.
                want = False
            else:
                want = controller.step(now, temperature, tout, target)

            if want != heater:
                heater = want
                log(
                    "Heating", "ON" if heater else "OFF",
                    "| Tin {} | Tout {} | target {}".format(
                        controller.tin_filtered, tout, target
                    )
                )

            set_servo_position(
                SERVO_ON_PERCENT if heater else SERVO_OFF_PERCENT
            )

            if model.samples != saved_samples:
                save_model(model)
                saved_samples = model.samples
                params = model.get_parameters(HEATER_POWER_WATTS)
                log(
                    "Thermal parameters: RC {:.1f} h, R {:.4f} C/W, "
                    "C {:.0f} kJ/C".format(
                        params["RC_hours"], params["R"],
                        params["C"] / 1000
                    )
                )

        except Exception as exc:
            log("Control-loop error:", exc)

            # The servo remains at its previous position.

        time.sleep(POLL_INTERVAL_SECONDS)


# ============================================================
# Entry point
# ============================================================

try:
    connect_wifi()
    main()

except Exception as exc:
    # Reboot and try again rather than sitting offline until someone
    # power-cycles the board. The OTA server keeps running during the
    # wait, so a fixed main.py can still be pushed.
    print("Fatal startup error:", exc)
    print("Rebooting in 30 seconds...")
    time.sleep(30)
    machine.reset()
