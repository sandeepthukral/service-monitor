"""Uptime Kuma -> AWTRIX 3 health tile bridge.

Scrapes Uptime Kuma's Prometheus /metrics endpoint (each monitor exposes a
`monitor_status` gauge: 1 up / 0 down / 2 pending / 3 maintenance) and pushes a
single custom app to an AWTRIX 3 clock (e.g. an Ulanzi TC001) over HTTP:

    green  "N/N"   all monitored services up
    red    "U/N"   one or more down (U = number up, N = total)

The tile is pushed with a `lifetime`, so if this bridge (or the whole host)
stops pushing, AWTRIX auto-removes the tile after `lifetime` seconds. A tile
that has vanished from the clock's rotation is therefore itself the "the
monitor host is down" signal, independent of any push we could send.

It never talks to the services directly — it only reads what Kuma has already
measured, so it stays fully decoupled from the checks themselves.

Run modes:
    python bridge.py          # push loop (production)
    python bridge.py --once   # single scrape + push, verbose, then exit
"""

import logging
import os
import re
import signal
import sys
import time

import requests

log = logging.getLogger("kuma-awtrix-bridge")

# AWTRIX custom-app colours (RGB hex).
COLOR_ALL_UP = "#00E000"    # everything up: green
COLOR_DOWN = "#FF3030"      # one or more down: red
COLOR_UNKNOWN = "#555555"   # no monitors / couldn't read Kuma: dim grey

# `monitor_status{...} <value>` — capture the trailing numeric sample value.
# Kuma's HELP/TYPE comment lines start with '#' and are skipped.
STATUS_RE = re.compile(r"^monitor_status\{.*\}\s+(\S+)$")

# monitor_status values that count as "up". 1 = up. 3 = maintenance (planned,
# not a failure) is treated as up so scheduled maintenance doesn't turn the
# clock red. 0 = down, 2 = pending both count as not-up.
UP_VALUES = {"1", "3"}


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def parse_counts(metrics_text: str) -> tuple[int, int]:
    """Return (up, total) parsed from a Kuma /metrics response body.

    A monitor is "up" when its monitor_status sample is in UP_VALUES. Samples
    that aren't a recognised number (e.g. NaN before the first probe) count
    toward total but not toward up, so a never-probed monitor reads as not-up.
    """
    up = total = 0
    for line in metrics_text.splitlines():
        m = STATUS_RE.match(line.strip())
        if not m:
            continue
        total += 1
        if m.group(1) in UP_VALUES:
            up += 1
    return up, total


def fetch_counts(metrics_url: str, token: str, timeout: float) -> tuple[int, int]:
    """Scrape Kuma /metrics and return (up, total).

    Kuma protects /metrics with HTTP Basic auth: the API key goes in the
    password field, and the username is ignored (blank here).
    """
    resp = requests.get(metrics_url, auth=("", token), timeout=timeout)
    resp.raise_for_status()
    return parse_counts(resp.text)


def build_payload(up: int, total: int, lifetime: int, icon: str = "") -> dict:
    """Build the AWTRIX custom-app payload for the health tile.

    `icon` is a LaMetric icon ID that must already be uploaded to the AWTRIX
    device; passing an ID the device doesn't have makes AWTRIX drop the tile,
    so an empty string omits the field entirely.
    """
    if total == 0:
        color = COLOR_UNKNOWN
    elif up == total:
        color = COLOR_ALL_UP
    else:
        color = COLOR_DOWN
    payload = {"text": f"{up}/{total}", "color": color, "lifetime": lifetime}
    if icon:
        payload["icon"] = icon
    return payload


def push_tile(base_url: str, name: str, payload: dict, timeout: float) -> None:
    resp = requests.post(
        f"{base_url}/api/custom",
        params={"name": name},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()


def run_once(metrics_url: str, token: str, base_url: str, app_name: str,
             lifetime: int, timeout: float, icon: str = "", push: bool = True) -> None:
    import json
    up, total = fetch_counts(metrics_url, token, timeout)
    payload = build_payload(up, total, lifetime, icon)
    print(f"Kuma monitors: {up}/{total} up")
    print(f"AWTRIX payload: {json.dumps(payload, indent=2)}")
    if push:
        push_tile(base_url, app_name, payload, timeout)
        print(f"Pushed '{app_name}' to {base_url}")


def run_loop(metrics_url: str, token: str, base_url: str, app_name: str,
             interval: int, lifetime: int, timeout: float, icon: str = "") -> None:
    running = True

    def stop(signum, _frame):
        nonlocal running
        log.info("Received signal %d, shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log.info("Pushing '%s' every %ds from %s -> %s (tile lifetime %ds)",
             app_name, interval, metrics_url, base_url, lifetime)

    consecutive_failures = 0
    while running:
        started = time.monotonic()
        try:
            up, total = fetch_counts(metrics_url, token, timeout)
            payload = build_payload(up, total, lifetime, icon)
            push_tile(base_url, app_name, payload, timeout)
            log.info("%s: %d/%d up", app_name, up, total)
            consecutive_failures = 0
        except Exception:
            # On failure we deliberately do NOT push: letting the tile expire is
            # the intended "host/Kuma is down" signal on the clock.
            consecutive_failures += 1
            log.exception("Cycle failed (%d consecutive); tile will expire if this persists",
                          consecutive_failures)

        # Back off on repeated failures (Kuma or clock unreachable), capped at
        # 5 min. Keep the cap below `lifetime` headroom so a recovered service
        # is reflected promptly.
        sleep_for = interval
        if consecutive_failures:
            sleep_for = min(interval * 2 ** min(consecutive_failures, 4), 300)
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + max(sleep_for - elapsed, 0)
        while running and time.monotonic() < deadline:
            time.sleep(1)

    log.info("Stopped")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    kuma_url = env("KUMA_URL").rstrip("/")           # e.g. http://uptime-kuma:3001
    kuma_token = env("KUMA_METRICS_TOKEN")           # Kuma API key (Basic-auth user)
    metrics_url = f"{kuma_url}/metrics"

    awtrix_host = env("AWTRIX_HOST").strip()
    base_url = awtrix_host if awtrix_host.startswith("http") else f"http://{awtrix_host}"
    base_url = base_url.rstrip("/")

    app_name = env("AWTRIX_APP_NAME", "health").strip()
    icon = env("HEALTH_ICON", "2259").strip()          # LaMetric icon ID (must be on the device)
    interval = int(env("PUSH_INTERVAL_SECONDS", "60"))
    lifetime = int(env("TILE_LIFETIME_SECONDS", "180"))
    timeout = float(env("HTTP_TIMEOUT_SECONDS", "10"))

    if lifetime <= interval:
        log.warning("TILE_LIFETIME_SECONDS (%d) <= PUSH_INTERVAL_SECONDS (%d): "
                    "the tile may flicker out between pushes; set lifetime to a "
                    "few push intervals.", lifetime, interval)

    if "--once" in sys.argv:
        run_once(metrics_url, kuma_token, base_url, app_name, lifetime, timeout,
                 icon=icon, push="--no-push" not in sys.argv)
    else:
        run_loop(metrics_url, kuma_token, base_url, app_name, interval, lifetime, timeout, icon)


if __name__ == "__main__":
    main()
