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
       │
   bridge (scrape /metrics) ──▶ AWTRIX clock  (green N/N | red U/N | gone = host down)
```

> **Offline note.** This runs on a NAS with **no internet access**, so an
> external dead-man's switch (healthchecks.io etc.) is impossible — nothing on
> the host can reach the internet. The host-down signal is instead the
> **self-expiring tile**: if the NAS / Docker / Kuma / bridge dies, the pushes
> stop and the tile vanishes from the clock within `TILE_LIFETIME_SECONDS`. The
> clock is separate LAN hardware, so it's a genuine independent failure domain
> and needs no internet. The trade-off: the signal is **visual** (a tile that
> disappears), not a push/email alert — an air-gapped host cannot notify you
> out-of-band. A missing tile is also ambiguous (host dead *or* clock off), but
> the two are obvious to tell apart by looking at the clock.

## Setup

1. **Configure**

   ```sh
   cp .env.example .env
   # edit .env: set AWTRIX_HOST; KUMA_METRICS_TOKEN comes from step 4.
   ```

2. **Start**

   ```sh
   mkdir -p kuma-data   # bind-mount target; Synology won't auto-create it
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

   The key is used as the HTTP Basic-auth **password** for `/metrics` (username
   blank) — the bridge handles this; just paste the key verbatim.

5. **Confirm the host-down signal.** With no internet there's no external
   dead-man's switch (see the offline note above); the self-expiring tile is it.
   Prove it works once:

   ```sh
   docker compose stop bridge
   # wait TILE_LIFETIME_SECONDS — the health tile should disappear from the clock
   docker compose up -d bridge   # tile returns
   ```

## Verify the bridge without the loop

```sh
docker compose run --rm bridge python bridge.py --once
# or dry run (scrape + print, no push):
docker compose run --rm bridge python bridge.py --once --no-push
```

## Monitors as code

`provision/monitors.yaml` is the source of truth for your monitors.
`provision/sync_monitors.py` logs into Kuma's API and reconciles it to match:
creates missing monitors, updates drifted ones (matched by name), and with
`--prune` removes managed monitors you've deleted from the file. Edit the YAML,
re-run, done — no clicking.

```sh
# needs KUMA_USERNAME / KUMA_PASSWORD in .env (your Kuma admin login)
docker compose run --rm provision --dry-run    # preview changes, touches nothing
docker compose run --rm provision              # apply create/update
docker compose run --rm provision --prune      # also delete managed extras
```

Notes:
- The tool only touches monitors whose **name** matches an entry in the file.
  Monitors you made by hand in the UI with other names are left alone, and
  `--prune` only deletes ones this tool created (tagged in their description).
- Supported types: `http`, `port`, `ping`, `docker`, `push`. A `docker` monitor
  references a `docker_host` by name; the tool auto-creates the host from the
  `docker_hosts:` block.
- For a `push` monitor (e.g. the alphaess-collector heartbeat), get its push URL
  from the **Kuma UI** (open the monitor). The tool logs a URL on creation, but
  some Kuma versions settle the token a moment later, so the UI is authoritative.
  Consumers on **other** Docker networks (like the collector) must reach it via
  the host LAN IP, not the internal service name.
- **You can still use the UI.** Adding monitors by hand and adding them as code
  aren't exclusive; the YAML just lets tomorrow's setup be repeatable. Anything
  you want reproducible, put in the file.
- `provision/monitors.yaml` is **bind-mounted** into the tool, so editing it
  takes effect on the next `run` — no image rebuild. (You only need to rebuild
  the `provision` image when `sync_monitors.py` or its deps change, e.g. after a
  `git pull` that touches them: `docker compose build provision`.)
- `uptime-kuma-api` is pinned in `provision/requirements.txt` and must match your
  Kuma server version — bump them together.

## Config

All via `.env` (see `.env.example`). Required for the bridge:
`KUMA_METRICS_TOKEN`, `AWTRIX_HOST`. Required for the provision tool:
`KUMA_USERNAME`, `KUMA_PASSWORD`. `KUMA_URL` is set by compose to the internal
service address.

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

Nothing else changes — same `.env`, same clock. Once it's on the Pi (a separate
box from the services it watches) the monitor lives in a real independent
failure domain: a NAS crash no longer takes the monitor down with it, so the
Pi can still report the NAS as down. The self-expiring tile stays the host-down
signal for the Pi itself. If the Pi ever gets internet, an external dead-man's
switch (healthchecks.io) becomes possible as a belt-and-suspenders add-on.

## Notes

- Kuma `monitor_status` values: `1` up, `0` down, `2` pending, `3` maintenance.
  The bridge counts **1 and 3** as up, so planned maintenance doesn't turn the
  clock red.
- `TILE_LIFETIME_SECONDS` must be comfortably larger than
  `PUSH_INTERVAL_SECONDS` or the tile flickers out between pushes; the bridge
  warns on startup if it isn't.
- The bridge only *reads* Kuma. It never contacts the monitored services, so it
  adds no load and can't itself be a false source of "up".
