import os
import time
import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import board
import adafruit_dht

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SENSOR_PIN = int(os.environ.get("SENSOR_PIN", "2"))
PORT       = int(os.environ.get("PORT", "5001"))
INTERVAL   = 300  # seconds between scheduled reads

_dht = adafruit_dht.DHT22(getattr(board, f"D{SENSOR_PIN}"))

_lock        = threading.Lock()  # protects _reading
_sensor_lock = threading.Lock()  # prevents concurrent sensor reads
_reading: dict | None = None     # {"temperature", "humidity", "recorded_at"}


# ── HTML pages ────────────────────────────────────────────────────────────────

def _page_no_data() -> bytes:
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="10">
  <title>Sensor — no data</title>
</head>
<body>
<pre>
No reading available yet.

Possible reasons:
  - Sensor still initialising (first read takes a few seconds after startup)
  - Wrong GPIO pin: currently configured as pin {SENSOR_PIN}
    Set the SENSOR_PIN environment variable if your wiring differs
  - Sensor not connected or wiring fault on GPIO {SENSOR_PIN}
  - Check container logs for errors: docker logs &lt;container-name&gt;

Collector config
  Port      : {PORT}
  GPIO pin  : {SENSOR_PIN}
  Interval  : {INTERVAL}s

Page refreshes automatically every 10 seconds.
</pre>
</body>
</html>""".encode()


def _page(reading: dict) -> bytes:
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="30">
  <title>Sensor</title>
</head>
<body>
  <pre>
Temperature : {reading['temperature']} °C
Humidity    : {reading['humidity']} %
Recorded    : {reading['recorded_at']}
  </pre>
</body>
</html>""".encode()


# ── Sensor reading ─────────────────────────────────────────────────────────────

def _raw_read():
    try:
        _dht.temperature  # warmup
    except RuntimeError:
        pass
    time.sleep(1)
    return _dht.temperature, _dht.humidity


def take_reading() -> dict | None:
    """Read the sensor, update the cache, and return the result.

    Acquires _sensor_lock so concurrent calls serialise rather than
    both hitting the hardware at the same time.
    """
    global _reading
    with _sensor_lock:
        try:
            temperature, humidity = _raw_read()
            result = {
                "temperature": round(float(temperature), 2),
                "humidity":    round(float(humidity), 2),
                "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            with _lock:
                _reading = result
            log.info("temp=%.1f°C  humidity=%.1f%%", temperature, humidity)
            return result
        except Exception as exc:
            log.error("Sensor read error on GPIO pin %d: %s", SENSOR_PIN, exc)
            return None


def sensor_loop():
    log.info("Sensor loop starting — pin=%d  interval=%ds", SENSOR_PIN, INTERVAL)
    while True:
        take_reading()
        time.sleep(INTERVAL)


# ── HTTP handler ───────────────────────────────────────────────────────────────

def _no_data_response() -> dict:
    return {
        "error": "No reading available yet",
        "debug": {
            "sensor_pin": SENSOR_PIN,
            "port": PORT,
            "hint": (
                f"Sensor has not produced a valid reading on GPIO pin {SENSOR_PIN}. "
                f"Check wiring and the SENSOR_PIN env var. "
                f"If the server cannot reach this device, confirm DEVICE_PORT={PORT} "
                f"in the server's devices.yaml matches this port."
            ),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/reading", "/reading/"):
            # Return the cached reading — fast, no hardware access
            with _lock:
                current = _reading
            if current is None:
                self._json(_no_data_response(), 503)
            else:
                self._json({"temperature": current["temperature"], "humidity": current["humidity"]}, 200)

        elif self.path == "/":
            with _lock:
                current = _reading
            if current is None:
                self._send(200, "text/html; charset=utf-8", _page_no_data())
            else:
                self._send(200, "text/html; charset=utf-8", _page(current))

        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path in ("/reading", "/reading/"):
            # Force an immediate fresh sensor read
            result = take_reading()
            if result is None:
                self._json(_no_data_response(), 503)
            else:
                self._json({"temperature": result["temperature"], "humidity": result["humidity"]}, 200)
        else:
            self._json({"error": "Not found"}, 404)

    def _json(self, data: dict, status: int):
        self._send(status, "application/json", json.dumps(data).encode())

    def _send(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("HTTP " + fmt, *args)


def main():
    threading.Thread(target=sensor_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("Listening on port %d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
