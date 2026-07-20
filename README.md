# service-monitor

Self-hosted uptime monitoring for home Docker services, with a health tile
pushed to an AWTRIX 3 clock.

Two containers:

- **uptime-kuma** — does the actual checks (HTTP / TCP / ping / Docker container),
  keeps history, and serves the dashboard + status page at `:3001`.
- **bridge** — scrapes Kuma's Prometheus `/metrics`, counts up vs. total, and
  pushes a single custom app to the clock:
  - green `N/N` when everything is up,
  - red `U/N` when one or more are down.

The tile is pushed with a `lifetime`, so if the bridge or the whole host stops
pushing, AWTRIX auto-removes the tile. **A missing health tile is itself the
"the monitor host is down" signal** — the one thing a monitor running next to
the services it watches can't otherwise tell you.

```
[ this host ]                              [ services ]
  uptime-kuma  ──HTTP/TCP/ping/docker──▶  containers
       │  (push heartbeat)
       ├──────────────────────▶ healthchecks.io ──▶ email if this host dies
       │
   bridge (scrape /metrics) ──▶ AWTRIX clock  (green N/N | red U/N | gone = host down)
```

## Setup

1. **Configure**

   ```sh
   cp .env.example .env
   # edit .env: set AWTRIX_HOST; KUMA_METRICS_TOKEN comes from step 4.
   ```

2. **Start**

   ```sh
   docker compose up -d
   ```

3. **Add monitors** in the Kuma UI (`http://<host>:3001`, create the admin
   account on first load). For each service add:
   - an **HTTP(s)** monitor ("is the app responding?"), and
   - for important ones, a **Docker Container** monitor ("is the container even
     running?"). Add a Docker host pointing at `/var/run/docker.sock`.

   Set the check interval to 60s (or whatever you like).

4. **Create the API key** — Settings → API Keys → Add. Put it in `.env` as
   `KUMA_METRICS_TOKEN`, then restart the bridge:

   ```sh
   docker compose up -d bridge
   ```

5. **Dead-man's switch** (do not skip while this runs on the NAS) — add a
   **Push** monitor in Kuma, paste its ping URL into a free
   [healthchecks.io](https://healthchecks.io) check, and set that check to
   email you. If the host / Docker / Kuma dies, the pings stop and healthchecks
   alerts you — the alert path that can't be running on the box that just died.

## Verify the bridge without the loop

```sh
docker compose run --rm bridge python bridge.py --once
# or dry run (scrape + print, no push):
docker compose run --rm bridge python bridge.py --once --no-push
```

## Config

All via `.env` (see `.env.example`). Required: `KUMA_METRICS_TOKEN`,
`AWTRIX_HOST`. `KUMA_URL` is set by compose to the internal service address.

## Moving to a Raspberry Pi later

This whole folder is self-contained. To migrate off the NAS:

```sh
# on the NAS
docker compose down
# copy the folder INCLUDING kuma-data/ (the history + config) to the Pi
rsync -a ./ pi@raspberrypi:~/service-monitor/
# on the Pi
cd ~/service-monitor && docker compose up -d
```

Nothing else changes — same `.env`, same clock. Once it's on the Pi it lives in
a real independent failure domain and the healthchecks.io switch becomes
belt-and-suspenders rather than load-bearing.

## Notes

- Kuma `monitor_status` values: `1` up, `0` down, `2` pending, `3` maintenance.
  The bridge counts **1 and 3** as up, so planned maintenance doesn't turn the
  clock red.
- `TILE_LIFETIME_SECONDS` must be comfortably larger than
  `PUSH_INTERVAL_SECONDS` or the tile flickers out between pushes; the bridge
  warns on startup if it isn't.
- The bridge only *reads* Kuma. It never contacts the monitored services, so it
  adds no load and can't itself be a false source of "up".
