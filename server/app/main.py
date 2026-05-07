import asyncio
import csv
import io
import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from . import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DEVICES_FILE       = os.environ.get("DEVICES_FILE", "/app/devices.yaml")
DEVICE_PORT        = int(os.environ.get("DEVICE_PORT", "5001"))
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL", "300"))
ALERT_URL          = os.environ.get("ALERT_URL", "")
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "0"))
_TZ_NAME           = os.environ.get("DISPLAY_TZ", "UTC")
LATITUDE           = os.environ.get("LATITUDE", "")
LONGITUDE          = os.environ.get("LONGITUDE", "")
LOCATION_NAME      = os.environ.get("LOCATION_NAME", "Outdoor")
_OPEN_METEO_URL    = "https://api.open-meteo.com/v1/forecast"

try:
    _DISPLAY_TZ = ZoneInfo(_TZ_NAME)
except ZoneInfoNotFoundError:
    log.warning("Unknown timezone %r, falling back to UTC", _TZ_NAME)
    _DISPLAY_TZ = ZoneInfo("UTC")
    _TZ_NAME = "UTC"

_TIMEOUT_CACHED = 10.0
_TIMEOUT_FRESH  = 20.0

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["localtime"] = lambda dt: dt.astimezone(_DISPLAY_TZ) if dt else dt

_poll_lock = asyncio.Lock()

# Per-device state (in-memory, reset on server restart)
_device_intervals:    dict[str, int]      = {}
_device_next_poll:    dict[str, datetime] = {}
_last_success:        dict[str, datetime] = {}
_consecutive_failures: dict[str, int]     = {}
_alert_cooldown:      dict[str, datetime] = {}
_last_cleanup:        datetime | None     = None

# devices.yaml mtime cache — avoids re-parsing on every poll tick
_devices_cache:     dict          = {}
_devices_cache_mtime: float | None = None


def load_devices() -> dict:
    global _devices_cache, _devices_cache_mtime
    try:
        mtime = Path(DEVICES_FILE).stat().st_mtime
        if mtime == _devices_cache_mtime:
            return _devices_cache
        with open(DEVICES_FILE) as f:
            data = yaml.safe_load(f)
        _devices_cache = {d["name"]: d for d in (data or {}).get("devices", [])}
        _devices_cache_mtime = mtime
        return _devices_cache
    except FileNotFoundError:
        log.error("devices.yaml not found at %s", DEVICES_FILE)
        return {}
    except Exception as exc:
        log.error("Failed to load devices.yaml: %s", exc)
        return {}


async def _send_alert(url: str, title: str, message: str) -> None:
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                url,
                content=message.encode(),
                headers={"Title": title, "Priority": "high", "Content-Type": "text/plain"},
                timeout=10.0,
            )
        log.info("Alert sent: %s", title)
    except Exception as exc:
        log.error("Failed to send alert to %s: %s", url, exc)


async def _check_threshold_alerts(name: str, device: dict, temp: float, hum: float) -> None:
    if not ALERT_URL:
        return
    now = datetime.now(timezone.utc)
    location = device.get("location", name)

    def cooldown_ok(key: str) -> bool:
        last = _alert_cooldown.get(key)
        return last is None or (now - last) > timedelta(hours=1)

    checks = [
        (f"{name}:temp_high", device.get("alert_temp_max"),
         lambda v: temp >= float(v),
         f"{location}: temperature {temp:.1f}°C exceeds maximum {{}}°C"),
        (f"{name}:temp_low",  device.get("alert_temp_min"),
         lambda v: temp <= float(v),
         f"{location}: temperature {temp:.1f}°C is below minimum {{}}°C"),
        (f"{name}:hum_high",  device.get("alert_humidity_max"),
         lambda v: hum >= float(v),
         f"{location}: humidity {hum:.1f}% exceeds maximum {{}}%"),
        (f"{name}:hum_low",   device.get("alert_humidity_min"),
         lambda v: hum <= float(v),
         f"{location}: humidity {hum:.1f}% is below minimum {{}}%"),
    ]

    for key, limit, violated, msg_template in checks:
        if limit is not None and violated(limit) and cooldown_ok(key):
            _alert_cooldown[key] = now
            await _send_alert(ALERT_URL, f"{location} — sensor alert", msg_template.format(limit))


def _device_status(name: str) -> str:
    fails = _consecutive_failures.get(name, 0)
    if fails >= 3:
        return "error"
    if fails >= 1:
        return "warning"
    last = _last_success.get(name)
    if last is None:
        return "unknown"
    age_secs = (datetime.now(timezone.utc) - last).total_seconds()
    interval = _device_intervals.get(name, POLL_INTERVAL)
    if age_secs > 3 * interval:
        return "error"
    if age_secs > 1.5 * interval:
        return "warning"
    return "ok"


async def _poll_device(client: httpx.AsyncClient, name: str, device: dict, force: bool) -> dict:
    """Poll a single device. Returns a result dict with status 'ok' or 'error'."""
    ip       = device.get("ip")
    location = device.get("location", name)

    if not ip:
        return {"name": name, "location": location, "status": "error",
                "error": "No IP configured in devices.yaml"}

    url     = f"http://{ip}:{DEVICE_PORT}/reading"
    timeout = _TIMEOUT_FRESH if force else _TIMEOUT_CACHED
    method  = "POST" if force else "GET"

    try:
        res = await client.request(method, url, timeout=timeout)

        try:
            data = res.json()
        except Exception:
            data = {}

        if res.status_code == 200:
            temp = data.get("temperature")
            hum  = data.get("humidity")
            if temp is not None and hum is not None:
                temp = float(temp) + float(device.get("temp_offset", 0))
                hum  = float(hum)  + float(device.get("humidity_offset", 0))

                recorded_at = datetime.now(timezone.utc)
                database.insert_reading(name, location, temp, hum, recorded_at)
                log.info("Polled %s (%s): temp=%.1f  hum=%.1f", name, location, temp, hum)

                _last_success[name]         = recorded_at
                _consecutive_failures[name] = 0

                await _check_threshold_alerts(name, device, temp, hum)

                return {
                    "name": name, "location": location, "status": "ok",
                    "temperature": round(temp, 1),
                    "humidity":    round(hum,  1),
                }
            else:
                error = "Device returned 200 but no sensor values — check collector logs"
                log.warning("Device %s: 200 but incomplete data: %s", name, data)
                _consecutive_failures[name] = _consecutive_failures.get(name, 0) + 1
                return {"name": name, "location": location, "status": "error", "error": error}

        else:
            debug = data.get("debug", {})
            hint  = debug.get("hint") if isinstance(debug, dict) else None
            error = hint or data.get("error") or f"Device returned HTTP {res.status_code}"
            log.warning("Device %s returned HTTP %d: %s", name, res.status_code, error)
            _consecutive_failures[name] = _consecutive_failures.get(name, 0) + 1
            return {"name": name, "location": location, "status": "error", "error": error}

    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        if isinstance(exc, httpx.ConnectError):
            error = (
                f"Could not connect to {ip} on port {DEVICE_PORT}. "
                "Check the device is running and the IP in devices.yaml is correct."
            )
            log.error("Connection refused: %s (%s:%d)", name, ip, DEVICE_PORT)
        else:
            error = f"Connection to {ip}:{DEVICE_PORT} timed out after {int(timeout)} s."
            log.error("Timeout polling %s (%s)", name, ip)

        fails = _consecutive_failures.get(name, 0) + 1
        _consecutive_failures[name] = fails

        if ALERT_URL and fails == 1:
            key = f"{name}:offline"
            now = datetime.now(timezone.utc)
            last = _alert_cooldown.get(key)
            if last is None or (now - last) > timedelta(hours=1):
                _alert_cooldown[key] = now
                await _send_alert(ALERT_URL, f"{location} — device offline", error)

        return {"name": name, "location": location, "status": "error", "error": error}

    except Exception as exc:
        log.error("Unexpected error polling %s (%s): %s", name, ip, exc)
        _consecutive_failures[name] = _consecutive_failures.get(name, 0) + 1
        return {"name": name, "location": location, "status": "error", "error": str(exc)}


def _advance_next_poll(name: str) -> None:
    interval = _device_intervals.get(name, POLL_INTERVAL)
    _device_next_poll[name] = datetime.now(timezone.utc) + timedelta(seconds=interval)


async def do_poll(force: bool = False) -> list[dict]:
    """Poll every device and write readings to the database."""
    async with _poll_lock:
        devices = load_devices()
        if not devices:
            return [{"name": "—", "location": "—", "status": "error",
                     "error": "No devices found in devices.yaml"}]

        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(*[
                _poll_device(client, name, device, force)
                for name, device in devices.items()
            ])

        if force:
            for name in devices:
                _advance_next_poll(name)

        return results


async def poll_devices():
    """Background task: poll each device on its own schedule; run daily retention cleanup."""
    global _last_cleanup
    await asyncio.sleep(5)

    now = datetime.now(timezone.utc)
    for name in load_devices():
        _device_intervals.setdefault(name, POLL_INTERVAL)
        _device_next_poll.setdefault(name, now)

    while True:
        now = datetime.now(timezone.utc)
        devices = load_devices()

        for name in devices:
            _device_intervals.setdefault(name, POLL_INTERVAL)
            _device_next_poll.setdefault(name, now)

        due = {n: d for n, d in devices.items() if now >= _device_next_poll[n]}
        if due:
            async with _poll_lock:
                async with httpx.AsyncClient() as client:
                    await asyncio.gather(*[
                        _poll_device(client, name, device, force=False)
                        for name, device in due.items()
                    ])
                for name in due:
                    _advance_next_poll(name)

        if DATA_RETENTION_DAYS > 0:
            if _last_cleanup is None or (now - _last_cleanup) > timedelta(hours=24):
                try:
                    deleted = database.delete_old_readings(DATA_RETENTION_DAYS)
                    if deleted:
                        log.info("Retention: deleted %d readings older than %d days",
                                 deleted, DATA_RETENTION_DAYS)
                except Exception as exc:
                    log.error("Retention cleanup failed: %s", exc)
                _last_cleanup = now

        await asyncio.sleep(15)


async def poll_weather() -> None:
    """Fetch current conditions from Open-Meteo every hour and store them."""
    if not LATITUDE or not LONGITUDE:
        log.info("Weather polling disabled — set LATITUDE and LONGITUDE to enable")
        return
    await asyncio.sleep(10)
    while True:
        try:
            params = {
                "latitude":        LATITUDE,
                "longitude":       LONGITUDE,
                "current":         "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                "wind_speed_unit": "mph",
                "forecast_days":   1,
            }
            async with httpx.AsyncClient() as client:
                res  = await client.get(_OPEN_METEO_URL, params=params, timeout=15.0)
                data = res.json()
            cur  = data.get("current", {})
            temp = cur.get("temperature_2m")
            hum  = cur.get("relative_humidity_2m")
            code = cur.get("weather_code")
            wind = cur.get("wind_speed_10m")
            if temp is not None and hum is not None:
                database.insert_weather(
                    float(temp), float(hum), code,
                    float(wind) if wind is not None else None,
                    datetime.now(timezone.utc),
                )
                log.info("Weather: %.1f°C  %.0f%%  code=%s", temp, hum, code)
        except Exception as exc:
            log.error("Weather poll failed: %s", exc)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_pool()
    database.init_db()
    database.init_weather_table()
    asyncio.create_task(poll_devices())
    asyncio.create_task(poll_weather())
    yield


app = FastAPI(lifespan=lifespan)


@app.post("/poll")
async def force_poll():
    """Trigger an immediate fresh read from every device."""
    results = await do_poll(force=True)
    return {"results": results}


@app.post("/interval/{device_name}")
async def set_device_interval(device_name: str, request: Request):
    """Update the poll interval for a single device (in seconds)."""
    devices = load_devices()
    if device_name not in devices:
        return JSONResponse({"error": "Device not found"}, status_code=404)
    try:
        body = await request.json()
        interval = int(body["interval"])
        if interval < 10:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        return JSONResponse({"error": "interval must be an integer >= 10"}, status_code=400)

    _device_intervals[device_name] = interval
    _device_next_poll[device_name] = datetime.now(timezone.utc) + timedelta(seconds=interval)
    log.info("Interval for %s set to %ds", device_name, interval)
    return {"ok": True, "interval": interval}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    devices = load_devices()
    latest  = {r["device_name"]: r for r in database.get_latest_readings()}
    now     = datetime.now(timezone.utc)

    next_polls = {}
    for name in devices:
        nxt = _device_next_poll.get(name)
        if nxt and nxt > now:
            secs      = int((nxt - now).total_seconds())
            local_nxt = nxt.astimezone(_DISPLAY_TZ)
            next_polls[name] = f"in {secs}s  ({local_nxt.strftime('%H:%M:%S')} {_TZ_NAME})"
        elif nxt:
            next_polls[name] = "due now"
        else:
            next_polls[name] = "—"

    intervals = {name: _device_intervals.get(name, POLL_INTERVAL) for name in devices}
    statuses  = {name: _device_status(name) for name in devices}

    return templates.TemplateResponse(request, "dashboard.html", {
        "devices":    devices,
        "latest":     latest,
        "next_polls": next_polls,
        "intervals":  intervals,
        "statuses":   statuses,
    })


@app.get("/api/weather")
def current_weather():
    if not LATITUDE or not LONGITUDE:
        return {"available": False, "configured": False}
    latest = database.get_latest_weather()
    if not latest:
        return {"available": False, "configured": True}
    return {
        "available":    True,
        "configured":   True,
        "location":     LOCATION_NAME,
        "temperature":  latest["temperature"],
        "humidity":     latest["humidity"],
        "weather_code": latest["weather_code"],
        "wind_speed":   latest["wind_speed"],
        "recorded_at":  latest["recorded_at"].isoformat(),
    }


@app.get("/api/weather/history")
def weather_history(hours: int = 24, start: str | None = None, end: str | None = None):
    if start and end:
        try:
            start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            end_dt   = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            return JSONResponse({"error": "Invalid date. Use YYYY-MM-DD."}, status_code=400)
        rows = database.get_weather_history(start=start_dt, end=end_dt)
    else:
        rows = database.get_weather_history(hours=hours)
    return [
        {"temperature": r["temperature"], "recorded_at": r["recorded_at"].isoformat()}
        for r in rows
    ]


@app.get("/api/summary")
def global_summary():
    return database.get_global_summary()


@app.get("/api/stats")
def device_stats(device: str, hours: int = 24, start: str | None = None, end: str | None = None):
    now = datetime.now(timezone.utc)
    if start and end:
        try:
            start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            end_dt   = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            return JSONResponse({"error": "Invalid date. Use YYYY-MM-DD."}, status_code=400)
    else:
        start_dt = now - timedelta(hours=hours)
        end_dt   = now
    result = database.get_stats(device, start_dt, end_dt)
    return result if result is not None else {"no_data": True}


@app.get("/api/dates")
def available_dates(device: str | None = None):
    """Return UTC dates that have at least one reading, optionally filtered by device."""
    return {"dates": database.get_dates_with_data(device)}


@app.get("/api/history")
def history(device: str, hours: int = 24, start: str | None = None, end: str | None = None):
    if start and end:
        try:
            start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            end_dt   = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            return JSONResponse({"error": "Invalid date. Use YYYY-MM-DD."}, status_code=400)
        rows = database.get_history_range(device, start_dt, end_dt)
    else:
        rows = database.get_history(device, hours)
    return [
        {
            "temperature": r["temperature"],
            "humidity":    r["humidity"],
            "recorded_at": r["recorded_at"].isoformat(),
        }
        for r in rows
    ]


@app.get("/api/export")
def export_csv(device: str, start: str | None = None, end: str | None = None):
    """Export raw readings as CSV. Defaults to the last 30 days if no range given."""
    now = datetime.now(timezone.utc)
    if start and end:
        try:
            start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            end_dt   = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            return JSONResponse({"error": "Invalid date. Use YYYY-MM-DD."}, status_code=400)
    else:
        start_dt = now - timedelta(days=30)
        end_dt   = now

    rows = database.get_export_data(device, start_dt, end_dt)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["recorded_at", "device_name", "location", "temperature_c", "humidity_pct"])
    for r in rows:
        writer.writerow([
            r["recorded_at"].isoformat(),
            r["device_name"],
            r["location"],
            r["temperature"],
            r["humidity"],
        ])

    filename = f"{device}_{start_dt.date()}_{end_dt.date()}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
