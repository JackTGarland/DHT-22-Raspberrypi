#!/usr/bin/env python3
"""Quick test: read DHT22 once and print the result."""

import sys
import board
import adafruit_dht

PIN = board.D2

print(f"Reading DHT22 on GPIO pin {PIN} ...")

dht = adafruit_dht.DHT22(PIN)

try:
    temperature = dht.temperature
    humidity = dht.humidity
except RuntimeError as e:
    print(f"ERROR: {e} — check wiring and pin number")
    sys.exit(1)
finally:
    dht.exit()

if temperature is None or humidity is None:
    print("ERROR: No data returned — check wiring and pin number")
    sys.exit(1)

print(f"Temperature : {temperature:.1f} °C")
print(f"Humidity    : {humidity:.1f} %")
