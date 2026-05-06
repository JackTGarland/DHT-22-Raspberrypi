# Temperature & Humidity Monitoring System

A self-hosted monitoring system for DHT22 sensors. A central server polls all devices on configurable schedules, stores readings in PostgreSQL, and serves a dashboard with live readings, historical charts, and per-period statistics.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Setup & Deployment](#setup--deployment)
   - [Server](#1-server)
   - [Raspberry Pi Collector](#2-raspberry-pi-collector)
   - [Pi Pico W](#3-pi-pico-w)
3. [Registering Devices](#registering-devices)
4. [Architecture](#architecture)
5. [Configuration Reference](#configuration-reference)
6. [Dashboard](#dashboard)
7. [API](#api)
8. [Database Schema](#database-schema)
9. [Updating](#updating)

---

## Prerequisites

| Component | Requirement |
|-----------|-------------|
| Server | Docker + Docker Compose |
| Raspberry Pi | Docker (any Pi model with a 40-pin header) |
| Pi Pico W | Thonny IDE, MicroPython firmware |
| Network | All devices on the same local network as the server |

---

## Setup & Deployment

### 1. Server

The server runs a FastAPI app and PostgreSQL in Docker Compose.

**Step 1 — Clone the repository and enter the server directory:**
```bash
git clone <repo-url>
cd temperature/server
```

**Step 2 — Create the environment file:**
```bash
cp .env.example .env
```
Open `.env` and set a strong password:
```
POSTGRES_PASSWORD=your_secure_password_here
```

**Step 3 — Add your devices to `devices.yaml`:**
```yaml
devices:
  - name: office         # unique key used in the database
    ip: 192.168.1.101    # device IP on your local network
    location: Office     # label shown on the dashboard
```
The server reads this file on every poll cycle — changes take effect without a restart.

**Step 4 — Start the stack:**
```bash
docker compose up -d
```

**Step 5 — Verify it started cleanly:**
```bash
docker compose logs -f app
```
You should see `Database pool ready` followed by the first poll attempt within 5 seconds. The dashboard is available at `http://<server-ip>:8065`.

---

### 2. Raspberry Pi Collector

One collector container runs per Pi. It reads the DHT22 sensor and serves the readings over HTTP on port 5001.

#### Hardware

Connect the DHT22 to the Pi's 40-pin header:

| DHT22 pin | Pi pin |
|-----------|--------|
| VCC | 3.3 V or 5 V |
| DATA | Any free GPIO (default: GPIO 2) |
| GND | Ground |

A 10 kΩ pull-up resistor between VCC and DATA is recommended.

#### Software

**Step 1 — Copy the collector files to the Pi:**
```bash
scp -r collector/ pi@<pi-ip>:~/collector
```

**Step 2 — Edit `docker-compose.yml` on the Pi** to match your wiring:
```yaml
environment:
  SENSOR_PIN: 2     # change to your GPIO pin number (BCM numbering)
  PORT: 5001
  INTERVAL: 300     # seconds between sensor reads
```

For a Pi 5, also update the `devices` section:
```yaml
devices:
  # Pi 1 / 2 / 3 / 4 / Zero / Zero 2:
  - /dev/gpiochip0:/dev/gpiochip0
  # Pi 5 — uncomment below and comment out the line above:
  # - /dev/gpiochip4:/dev/gpiochip4
```

**Step 3 — Build and start the collector:**
```bash
cd ~/collector
docker build -t collector:latest .
docker compose up -d
```

**Step 4 — Check it is reading the sensor:**
```bash
docker compose logs -f
curl http://localhost:5001/reading
```
A successful response looks like:
```json
{"temperature": 21.5, "humidity": 58.3}
```

**Step 5 — Add the Pi to `server/devices.yaml`** with its IP address (see [Registering Devices](#registering-devices)).

#### Deploying via Portainer

If you manage your Pi with Portainer, build the image first:
```bash
ssh pi@<pi-ip> "cd ~/collector && docker build -t collector:latest ."
```
Then in Portainer → select the Pi environment → Stacks → Add Stack → paste the contents of `collector/docker-compose.yml`, changing `build: .` to `image: collector:latest`.

---

### 3. Pi Pico W

The Pico W runs a lightweight MicroPython HTTP server — no Docker required.

**Step 1 — Flash MicroPython firmware** onto the Pico W if not already done.  
Download from [micropython.org/download/RPI_PICO_W](https://micropython.org/download/RPI_PICO_W/).

**Step 2 — Open Thonny** and connect to the Pico W (set interpreter to *MicroPython (Raspberry Pi Pico)* in the bottom-right corner).

**Step 3 — Create `secrets.py`** from the template:
```python
# secrets.py
WIFI_SSID     = "your_wifi_ssid"
WIFI_PASSWORD = "your_wifi_password"
```
In Thonny: File → Save as → MicroPython device → `secrets.py`.

**Step 4 — Edit `pi_pico_sensor_api.py`** at the top of the file:
```python
LOCATION   = "Living Room"  # informational label
SENSOR_PIN = 4              # GPIO pin the DHT22 data line is connected to
PORT       = 5001
```

**Step 5 — Deploy the script:**  
In Thonny: open `pi_pico_sensor_api.py` → File → Save as → MicroPython device → **`main.py`**.  
Saving as `main.py` makes it start automatically on every power-on.

**Step 6 — Note the IP address** printed to the Thonny console after a successful WiFi connection. The LED blinks continuously if WiFi fails.

**Step 7 — Add the Pico W to `server/devices.yaml`** with that IP address.

---

## Registering Devices

All devices (Pi collectors and Pico Ws) are registered in `server/devices.yaml`:

```yaml
devices:
  - name: office          # unique key — used as the identifier in the database
    ip: 192.168.1.101     # local network IP of the device
    location: Office      # human-readable label on the dashboard

  - name: living_room
    ip: 192.168.1.102
    location: Living Room

  - name: garage
    ip: 192.168.1.103
    location: Garage
```

- `name` must be unique and is used as the database key. Do not change it after data has been collected.
- `location` can be changed at any time — new readings will use the updated label.
- The file is read on every poll cycle, so new devices are picked up without a server restart.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│   Raspberry Pi (each)  — Docker container        │
│                                                  │
│   GET  /reading  →  cached reading (fast)        │
│   POST /reading  →  fresh sensor read on demand  │
└──────────────┬───────────────────────────────────┘
               │
┌──────────────────────────────────────────────────┐
│   Pi Pico W (each)  — MicroPython                │
│                                                  │
│   GET  /reading  →  fresh read (always)          │
│   POST /reading  →  fresh read (always)          │
└──────────────┬───────────────────────────────────┘
               │  port 5001 on each device
               ▼
┌─────────────────────────────────────────┐
│              Server                     │
│  ┌─────────────┐     ┌───────────────┐  │
│  │  FastAPI    │────▶│  PostgreSQL   │  │
│  │  dashboard  │◀────│               │  │
│  └─────────────┘     └───────────────┘  │
│         port 8065                       │
└─────────────────────────────────────────┘
```

The server is the **source of truth** for timestamps, device names, and poll schedules. Devices only return raw sensor values.

### Poll behaviour

| Trigger | Method | Raspberry Pi | Pi Pico W |
|---------|--------|--------------|-----------|
| Scheduled (per-device interval) | `GET /reading` | Returns cached value — no hardware hit | Fresh sensor read |
| **Refresh now** button | `POST /reading` | Forces an immediate sensor read | Fresh sensor read |

The Raspberry Pi collector caches readings between scheduled intervals so that `GET /reading` responds instantly without touching the hardware.

---

## Configuration Reference

### Server (`server/.env` and `server/docker-compose.yml`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `POSTGRES_PASSWORD` | yes | — | PostgreSQL password |
| `DEVICE_PORT` | no | `5001` | Port expected on each collector device |
| `POLL_INTERVAL` | no | `300` | Default poll interval in seconds (can be overridden per device in the dashboard) |
| `ALERT_URL` | no | `""` | ntfy.sh-compatible webhook URL for threshold and offline alerts |
| `DATA_RETENTION_DAYS` | no | `0` | Delete readings older than N days each day (`0` = keep forever) |
| `DISPLAY_TZ` | no | `UTC` | IANA timezone for dashboard timestamps (e.g. `Europe/London`) |

### Raspberry Pi Collector (`collector/docker-compose.yml`)

| Variable | Default | Description |
|----------|---------|-------------|
| `SENSOR_PIN` | `2` | BCM GPIO pin the DHT22 data line is connected to |
| `PORT` | `5001` | Port the collector HTTP server listens on |
| `INTERVAL` | `300` | Seconds between scheduled sensor reads |

### Pi Pico W (`pi_pico_sensor_api.py`)

| Constant | Default | Description |
|----------|---------|-------------|
| `LOCATION` | `""` | Informational label (dashboard uses `devices.yaml` location) |
| `SENSOR_PIN` | `4` | GPIO pin the DHT22 data line is connected to |
| `PORT` | `5001` | Port the HTTP server listens on |

### Per-device options (`server/devices.yaml`)

These optional fields can be added to any device entry:

| Field | Description |
|-------|-------------|
| `temp_offset` | Celsius value added to every raw temperature reading before storing (calibration) |
| `humidity_offset` | Percentage value added to every raw humidity reading before storing (calibration) |
| `alert_temp_max` | Send an alert when temperature rises above this value (°C) |
| `alert_temp_min` | Send an alert when temperature drops below this value (°C) |
| `alert_humidity_max` | Send an alert when humidity rises above this value (%) |
| `alert_humidity_min` | Send an alert when humidity drops below this value (%) |

Alerts also fire when a device becomes unreachable. A 1-hour cooldown prevents repeated notifications.

---

## Dashboard

Served at `http://<server-ip>:8065`.

### Current readings

One card per device showing the latest temperature, humidity, and reading timestamp. Each card also shows:
- A coloured **status dot**: green (ok), amber (recent failure or overdue), red (multiple failures or long overdue), grey (no data yet)
- The device IP address (from `devices.yaml`)
- When the next poll is scheduled (in the configured `DISPLAY_TZ`)
- An interval control to change how often that device is polled — takes effect immediately without a server restart

### Temperature units

The **°F** button in the header toggles all temperature displays between Celsius and Fahrenheit without re-fetching data.

### History charts

Dual-axis line charts (temperature on the left, % RH on the right) per device. The y-axis label and chart legend update when the unit is toggled.

**Quick ranges:** 1h / 6h / 24h / 7d — buttons in the top-right of the history section.

**Custom date range:** Two date inputs constrained to dates that actually have data. Selecting a date with no readings shows a validation error. Click **Apply** to load that range.

**Resolution:** The server automatically chooses an appropriate resolution:
- ≤ 48 hours → raw readings
- 48 h – 14 days → hourly averages
- > 14 days → daily averages

**Data gaps:** Breaks in data (e.g. sensor offline) are shown as visible gaps in the chart line rather than being interpolated.

**CSV export:** Each chart has a **↓ CSV** button that downloads the raw readings for that device and the currently selected date range.

### Statistics

Below each chart, a compact summary shows **average**, **high**, and **low** for both temperature and humidity over the currently displayed period. Statistics update automatically whenever the chart range changes and respect the current temperature unit toggle.

### Refresh now

Forces an immediate fresh sensor read from every device. Shows a per-device result row (green on success, red on failure) and reloads the page if all devices succeed.

The page auto-reloads every 5 minutes to keep the current-reading cards fresh.

---

## Alerting

Set `ALERT_URL` in `server/docker-compose.yml` to any ntfy.sh-compatible endpoint:

```yaml
ALERT_URL: "https://ntfy.sh/your-topic"
# or self-hosted: "http://192.168.1.10:8080/your-topic"
```

Alerts fire for:
- **Threshold breaches** — temperature or humidity outside the per-device limits set in `devices.yaml`
- **Device offline** — first consecutive failure to reach a device

Each alert type has a **1-hour cooldown** per device to prevent spam. The cooldown resets when the server restarts.

---

## API

### Server endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard HTML |
| `POST` | `/poll` | Immediately poll all devices; returns per-device results |
| `POST` | `/interval/{device_name}` | Update poll interval `{"interval": 120}` (seconds) |
| `GET` | `/api/history` | Reading history — see params below |
| `GET` | `/api/stats` | Aggregate statistics — see params below |
| `GET` | `/api/dates` | Dates that have data |
| `GET` | `/api/export` | Raw CSV export — see params below |

**`/api/history` params:**

| Param | Type | Description |
|-------|------|-------------|
| `device` | string | Device name (required) |
| `hours` | int | Rolling window in hours (default: 24) |
| `start` | YYYY-MM-DD | Range start date (UTC). Use with `end`. |
| `end` | YYYY-MM-DD | Range end date (UTC, inclusive). Use with `start`. |

History is automatically downsampled for long ranges (see Dashboard → History charts above). Use `/api/export` to get unsampled raw data.

**`/api/stats` params:** identical to `/api/history`.

**`/api/dates` params:**

| Param | Type | Description |
|-------|------|-------------|
| `device` | string | Filter to one device (optional — omit for all devices) |

**`/api/export` params:**

| Param | Type | Description |
|-------|------|-------------|
| `device` | string | Device name (required) |
| `start` | YYYY-MM-DD | Range start (UTC, inclusive). Defaults to 30 days ago. |
| `end` | YYYY-MM-DD | Range end (UTC, inclusive). Defaults to today. |

Returns a CSV file with columns: `recorded_at`, `device_name`, `location`, `temperature_c`, `humidity_pct`.

### Collector endpoints (port 5001 on each Raspberry Pi)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/reading` | Returns the most recent cached reading |
| `POST` | `/reading` | Forces an immediate sensor read and returns the result |
| `GET` | `/` | HTML status page showing last reading and config |

A `503` response includes a `debug.hint` field explaining the likely cause (wrong GPIO pin, wiring fault, etc.).

### Pi Pico W endpoints (port 5001)

| Method | Path | Description |
|--------|------|-------------|
| `GET` / `POST` | `/reading` | Fresh sensor read |
| `GET` | `/test` | Diagnostic info without the retry logic |
| `GET` | `/` | JSON status |

---

## Database Schema

```sql
CREATE TABLE readings (
    id          SERIAL PRIMARY KEY,
    device_name VARCHAR(64)   NOT NULL,
    location    VARCHAR(128)  NOT NULL DEFAULT '',
    temperature REAL          NOT NULL,
    humidity    REAL          NOT NULL,
    recorded_at TIMESTAMPTZ   NOT NULL
);

CREATE INDEX idx_readings_device_time ON readings (device_name, recorded_at DESC);
```

Timestamps are always set by the server in UTC. The `location` column was added via a non-destructive `ALTER TABLE … ADD COLUMN IF NOT EXISTS` migration, so existing databases upgrade automatically on server start.

---

## Updating

### Server

```bash
cd server
docker compose build --no-cache
docker compose up -d
```

### Raspberry Pi Collector

```bash
# On the Pi
cd ~/collector
docker build -t collector:latest .
docker compose up -d
```

### Pi Pico W

Open `pi_pico_sensor_api.py` in Thonny, make changes, and re-save to the Pico as `main.py`. Disconnect and reconnect power to restart.

---

## Repository Layout

```
temperature/
├── .gitignore
├── README.md
├── secrets.py.example              # Copy to secrets.py on each Pico W — never commit
├── pi_pico_sensor_api.py           # MicroPython — deploy to each Pico W as main.py
│
├── collector/                      # One Docker container per Raspberry Pi
│   ├── Dockerfile
│   ├── docker-compose.yml          # Edit SENSOR_PIN and INTERVAL per device
│   ├── collector.py
│   ├── requirements.txt
│   └── test_sensor.py              # Run directly on the Pi to verify sensor wiring
│
└── server/                         # Central server — Docker Compose stack
    ├── Dockerfile
    ├── docker-compose.yml
    ├── devices.yaml                # Device registry — add devices here
    ├── .env                        # POSTGRES_PASSWORD — never commit
    ├── .env.example
    └── app/
        ├── main.py
        ├── database.py
        └── templates/
            └── dashboard.html
```
