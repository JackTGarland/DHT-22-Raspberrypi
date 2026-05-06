import network
import json
from time import sleep
from machine import Pin
import socket
import dht

from secrets import WIFI_SSID, WIFI_PASSWORD

# --- Configuration — edit these for each device ---
LOCATION   = ""  # e.g. "Living Room" (informational only; server devices.yaml is authoritative)
SENSOR_PIN = 4   # GPIO pin the DHT22 data line is connected to
PORT       = 5001
# --------------------------------------------------

led = Pin("LED", Pin.OUT)


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        print("Already connected to WiFi")
        return True

    print(f"Connecting to {WIFI_SSID}...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    max_wait = 10
    while max_wait > 0:
        if wlan.isconnected():
            break
        max_wait -= 1
        print("Waiting for connection...")
        sleep(1)

    if wlan.isconnected():
        print(f"Connected — IP: {wlan.ifconfig()[0]}")
        return True

    print("Failed to connect to WiFi")
    return False


# Blink LED indefinitely on WiFi failure so the problem is visible
if not connect_wifi():
    while True:
        led.toggle()
        sleep(0.5)

dht_sensor = dht.DHT22(Pin(SENSOR_PIN))


def get_sensor_reading():
    try:
        sleep(0.5)
        for attempt in range(3):
            try:
                print(f"Reading attempt {attempt + 1}/3")
                dht_sensor.measure()
                temperature = dht_sensor.temperature()
                humidity    = dht_sensor.humidity()

                if temperature is not None and humidity is not None:
                    if -40 <= temperature <= 80 and 0 <= humidity <= 100:
                        return {
                            "temperature": round(float(temperature), 2),
                            "humidity":    round(float(humidity),    2),
                        }, 200
                    print(f"Out of range — temp: {temperature}, humidity: {humidity}")
                else:
                    print("Sensor returned None")

                if attempt < 2:
                    sleep(1)

            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    sleep(1)

        return {"error": "Failed to read sensor after 3 attempts"}, 500

    except Exception as e:
        return {"error": f"Sensor error: {str(e)}"}, 500


def test_sensor():
    try:
        dht_sensor.measure()
        return {
            "status":      "ok",
            "location":    LOCATION,
            "gpio_pin":    SENSOR_PIN,
            "temperature": dht_sensor.temperature(),
            "humidity":    dht_sensor.humidity(),
        }, 200
    except Exception as e:
        return {
            "status":   "error",
            "location": LOCATION,
            "gpio_pin": SENSOR_PIN,
            "error":    str(e),
        }, 500


_STATUS_PHRASES = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}

def create_response(data, status_code=200):
    body   = json.dumps(data)
    phrase = _STATUS_PHRASES.get(status_code, "Unknown")
    return (
        f"HTTP/1.1 {status_code} {phrase}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "\r\n"
        + body
    )


def handle_request(request):
    lines = request.split('\n')
    if lines:
        first = lines[0].strip()

        if first.startswith('POST'):
            if '/reading' in first:
                # Pico always reads fresh, so POST and GET are equivalent
                data, code = get_sensor_reading()
                return create_response(data, code)

        elif first.startswith('GET'):
            if '/reading' in first:
                data, code = get_sensor_reading()
                return create_response(data, code)
            elif '/test' in first:
                data, code = test_sensor()
                return create_response(data, code)
            elif '/' in first:
                return create_response({
                    "status":   "Pi Pico W Sensor API running",
                    "location": LOCATION,
                    "port":     PORT,
                })

    return create_response({"error": "Not found"}, 404)


def run_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('0.0.0.0', PORT))
        s.listen(1)
        print(f"Listening on port {PORT}")
        while True:
            try:
                cl, addr = s.accept()
                print('Client connected from', addr)
                response = handle_request(cl.recv(1024).decode())
                cl.send(response.encode())
                cl.close()
            except Exception as e:
                print("Request error:", e)
                try:
                    cl.close()
                except:
                    pass
    except OSError as e:
        if e.errno == 98:
            print(f"Port {PORT} already in use — restart the Pico W")
        else:
            print(f"Socket error: {e}")
    finally:
        try:
            s.close()
        except:
            pass


if __name__ == '__main__':
    run_server()
