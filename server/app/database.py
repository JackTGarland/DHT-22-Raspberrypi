import os
import time
import logging
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]
_pool: psycopg2.pool.SimpleConnectionPool | None = None

_RAW_HOURS    = 48    # ranges ≤ this use raw data
_HOURLY_HOURS = 336   # ranges ≤ this use hourly averages; beyond → daily averages


def init_pool(retries: int = 10, delay: int = 3) -> None:
    global _pool
    for attempt in range(1, retries + 1):
        try:
            _pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)
            log.info("Database pool ready")
            return
        except psycopg2.OperationalError as exc:
            log.warning("DB not ready (attempt %d/%d): %s", attempt, retries, exc)
            time.sleep(delay)
    raise RuntimeError(f"Could not connect to database after {retries} attempts")


def _get():
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_pool() first")
    return _pool.getconn()


def _put(conn) -> None:
    _pool.putconn(conn)


def init_db() -> None:
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS readings (
                    id          SERIAL PRIMARY KEY,
                    device_name VARCHAR(64)   NOT NULL,
                    location    VARCHAR(128)  NOT NULL DEFAULT '',
                    temperature REAL          NOT NULL,
                    humidity    REAL          NOT NULL,
                    recorded_at TIMESTAMPTZ   NOT NULL
                )
            """)
            cur.execute("""
                ALTER TABLE readings
                ADD COLUMN IF NOT EXISTS location VARCHAR(128) NOT NULL DEFAULT ''
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_readings_device_time
                ON readings (device_name, recorded_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_readings_temperature
                ON readings (temperature)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_readings_humidity
                ON readings (humidity)
            """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _put(conn)


def insert_reading(
    device_name: str,
    location: str,
    temperature: float,
    humidity: float,
    recorded_at: datetime,
) -> None:
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO readings (device_name, location, temperature, humidity, recorded_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (device_name, location, temperature, humidity, recorded_at),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _put(conn)


def get_latest_readings() -> list:
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (device_name)
                    device_name, location, temperature, humidity, recorded_at
                FROM readings
                ORDER BY device_name, recorded_at DESC
            """)
            return [dict(r) for r in cur.fetchall()]
    finally:
        _put(conn)


def get_dates_with_data(device_name: str | None = None) -> list[str]:
    """Return sorted list of UTC dates (YYYY-MM-DD) that have at least one reading."""
    conn = _get()
    try:
        with conn.cursor() as cur:
            if device_name:
                cur.execute("""
                    SELECT DISTINCT recorded_at::date AS day
                    FROM readings
                    WHERE device_name = %s
                    ORDER BY day
                """, (device_name,))
            else:
                cur.execute("""
                    SELECT DISTINCT recorded_at::date AS day
                    FROM readings
                    ORDER BY day
                """)
            return [row[0].isoformat() for row in cur.fetchall()]
    finally:
        _put(conn)


def get_stats(device_name: str, start: datetime, end: datetime) -> dict | None:
    """Return avg/max/min for temperature and humidity over a time window."""
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    AVG(temperature) AS avg_temp,
                    MAX(temperature) AS max_temp,
                    MIN(temperature) AS min_temp,
                    AVG(humidity)    AS avg_hum,
                    MAX(humidity)    AS max_hum,
                    MIN(humidity)    AS min_hum,
                    COUNT(*)         AS reading_count
                FROM readings
                WHERE device_name = %s
                  AND recorded_at >= %s
                  AND recorded_at < %s
            """, (device_name, start, end))
            row = cur.fetchone()
            if not row or not row["reading_count"]:
                return None
            return {
                "avg_temp": round(float(row["avg_temp"]), 1),
                "max_temp": round(float(row["max_temp"]), 1),
                "min_temp": round(float(row["min_temp"]), 1),
                "avg_hum":  round(float(row["avg_hum"]),  1),
                "max_hum":  round(float(row["max_hum"]),  1),
                "min_hum":  round(float(row["min_hum"]),  1),
                "count":    int(row["reading_count"]),
            }
    finally:
        _put(conn)


def _get_history_raw(device_name: str, start: datetime, end: datetime) -> list:
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT temperature, humidity, recorded_at
                FROM readings
                WHERE device_name = %s
                  AND recorded_at >= %s
                  AND recorded_at < %s
                ORDER BY recorded_at ASC
            """, (device_name, start, end))
            return [dict(r) for r in cur.fetchall()]
    finally:
        _put(conn)


def _get_history_aggregated(device_name: str, start: datetime, end: datetime, bucket: str) -> list:
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    DATE_TRUNC(%s, recorded_at) AS recorded_at,
                    AVG(temperature)::real       AS temperature,
                    AVG(humidity)::real          AS humidity
                FROM readings
                WHERE device_name = %s
                  AND recorded_at >= %s
                  AND recorded_at < %s
                GROUP BY DATE_TRUNC(%s, recorded_at)
                ORDER BY recorded_at ASC
            """, (bucket, device_name, start, end, bucket))
            return [dict(r) for r in cur.fetchall()]
    finally:
        _put(conn)


def _get_history_auto(device_name: str, start: datetime, end: datetime) -> list:
    """Return raw data for short ranges, hourly/daily averages for longer ones."""
    range_hours = (end - start).total_seconds() / 3600
    if range_hours <= _RAW_HOURS:
        return _get_history_raw(device_name, start, end)
    elif range_hours <= _HOURLY_HOURS:
        return _get_history_aggregated(device_name, start, end, "hour")
    else:
        return _get_history_aggregated(device_name, start, end, "day")


def get_history_range(device_name: str, start: datetime, end: datetime) -> list:
    return _get_history_auto(device_name, start, end)


def get_history(device_name: str, hours: int) -> list:
    end   = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    return _get_history_auto(device_name, start, end)


def get_export_data(device_name: str, start: datetime, end: datetime) -> list:
    """Always return raw (un-aggregated) readings for CSV export."""
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT device_name, location, temperature, humidity, recorded_at
                FROM readings
                WHERE device_name = %s
                  AND recorded_at >= %s
                  AND recorded_at < %s
                ORDER BY recorded_at ASC
            """, (device_name, start, end))
            return [dict(r) for r in cur.fetchall()]
    finally:
        _put(conn)


def init_weather_table() -> None:
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS weather (
                    id           SERIAL PRIMARY KEY,
                    temperature  REAL        NOT NULL,
                    humidity     REAL        NOT NULL,
                    weather_code INT,
                    wind_speed   REAL,
                    recorded_at  TIMESTAMPTZ NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_weather_time
                ON weather (recorded_at DESC)
            """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _put(conn)


def insert_weather(
    temperature: float,
    humidity: float,
    weather_code: int | None,
    wind_speed: float | None,
    recorded_at: datetime,
) -> None:
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO weather (temperature, humidity, weather_code, wind_speed, recorded_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (temperature, humidity, weather_code, wind_speed, recorded_at),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _put(conn)


def get_latest_weather() -> dict | None:
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT temperature, humidity, weather_code, wind_speed, recorded_at
                FROM weather ORDER BY recorded_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _put(conn)


def get_weather_history(
    hours: int = 24,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list:
    if start and end:
        s, e = start, end
    else:
        e = datetime.now(timezone.utc)
        s = e - timedelta(hours=hours)
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT temperature, recorded_at
                FROM weather
                WHERE recorded_at >= %s AND recorded_at < %s
                ORDER BY recorded_at ASC
            """, (s, e))
            return [dict(r) for r in cur.fetchall()]
    finally:
        _put(conn)


def get_global_summary() -> dict:
    """All-time records and cross-device 24-hour averages."""
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT temperature, location, recorded_at
                FROM readings ORDER BY temperature DESC LIMIT 1
            """)
            max_t = cur.fetchone()

            cur.execute("""
                SELECT temperature, location, recorded_at
                FROM readings ORDER BY temperature ASC LIMIT 1
            """)
            min_t = cur.fetchone()

            cur.execute("""
                SELECT humidity, location, recorded_at
                FROM readings ORDER BY humidity DESC LIMIT 1
            """)
            max_h = cur.fetchone()

            cur.execute("""
                SELECT
                    AVG(temperature) AS avg_temp,
                    AVG(humidity)    AS avg_hum,
                    COUNT(DISTINCT device_name) AS device_count
                FROM readings
                WHERE recorded_at >= NOW() - INTERVAL '24 hours'
            """)
            avg = cur.fetchone()

        out: dict = {}
        if max_t and max_t["temperature"] is not None:
            out["record_high_temp"] = {
                "value":    round(float(max_t["temperature"]), 1),
                "location": max_t["location"],
                "when":     max_t["recorded_at"].isoformat(),
            }
        if min_t and min_t["temperature"] is not None:
            out["record_low_temp"] = {
                "value":    round(float(min_t["temperature"]), 1),
                "location": min_t["location"],
                "when":     min_t["recorded_at"].isoformat(),
            }
        if max_h and max_h["humidity"] is not None:
            out["record_high_hum"] = {
                "value":    round(float(max_h["humidity"]), 1),
                "location": max_h["location"],
                "when":     max_h["recorded_at"].isoformat(),
            }
        if avg and avg["avg_temp"] is not None:
            out["avg_24h"] = {
                "avg_temp":     round(float(avg["avg_temp"]), 1),
                "avg_hum":      round(float(avg["avg_hum"]), 1),
                "device_count": int(avg["device_count"]),
            }
        return out
    finally:
        _put(conn)


def delete_old_readings(days: int) -> int:
    """Delete readings older than `days` days. Returns the number of rows deleted."""
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM readings WHERE recorded_at < NOW() - INTERVAL '1 day' * %s",
                (days,),
            )
            deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        _put(conn)
