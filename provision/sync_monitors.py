"""Reconcile Uptime Kuma monitors to match monitors.yaml (config as code).

monitors.yaml is the source of truth. This script logs into Kuma's API and:
  - creates monitors listed in the file that don't exist yet,
  - updates existing monitors (matched by name) whose managed fields drifted,
  - with --prune, deletes monitors that were created by this tool but are no
    longer in the file.

Only monitors whose name appears in the file are ever touched; monitors you
created by hand in the UI with other names are left alone (and --prune only
removes ones this tool manages — see MANAGED_TAG).

The API needs the Kuma ADMIN username/password (the API key used by the metrics
bridge does not grant API access). uptime-kuma-api is version-coupled to Kuma;
pin it in requirements.txt to match your uptime-kuma image.

Usage:
    python sync_monitors.py --dry-run          # show what would change
    python sync_monitors.py                    # apply create/update
    python sync_monitors.py --prune            # also delete managed extras
"""

import argparse
import logging
import os
import sys

import yaml
from uptime_kuma_api import UptimeKumaApi, MonitorType, DockerType

log = logging.getLogger("sync-monitors")

# Appended to the description of every monitor this tool creates, so --prune can
# tell "managed by code" apart from "created by hand in the UI".
MANAGED_TAG = "[managed:service-monitor]"

# Map our short YAML type names to Kuma's MonitorType enum.
TYPE_MAP = {
    "http": MonitorType.HTTP,
    "port": MonitorType.PORT,
    "ping": MonitorType.PING,
    "docker": MonitorType.DOCKER,
    "push": MonitorType.PUSH,
}

# Fields we manage per type (besides name/type/interval/retryInterval/maxretries
# which are common). Anything not listed here is left at Kuma's default and not
# diffed, so hand-tweaks to unmanaged fields survive.
TYPE_FIELDS = {
    "http": ["url"],
    "port": ["hostname", "port"],
    "ping": ["hostname"],
    "docker": ["docker_container", "docker_host"],  # docker_host resolved to id
    "push": [],
}

COMMON_FIELDS = ["interval", "retryInterval", "maxretries"]


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("defaults", {})
    cfg.setdefault("docker_hosts", [])
    cfg.setdefault("monitors", [])
    return cfg


def ensure_docker_hosts(api: UptimeKumaApi, hosts: list[dict], dry_run: bool) -> dict[str, int]:
    """Ensure each configured docker host exists; return {name: id}."""
    existing = {h["name"]: h["id"] for h in api.get_docker_hosts()}
    name_to_id: dict[str, int] = {}
    for h in hosts:
        name = h["name"]
        if name in existing:
            name_to_id[name] = existing[name]
            continue
        dtype = DockerType.SOCKET if h.get("type", "socket") == "socket" else DockerType.TCP
        log.info("Docker host '%s' missing -> create (%s %s)", name, h.get("type"), h.get("daemon"))
        if dry_run:
            name_to_id[name] = -1  # placeholder; real id assigned on apply
            continue
        result = api.add_docker_host(name=name, dockerType=dtype, dockerDaemon=h["daemon"])
        name_to_id[name] = result["id"]
    return name_to_id


def desired_monitor(m: dict, defaults: dict, docker_hosts: dict[str, int]) -> dict:
    """Build the kwargs dict for a monitor from its YAML entry + defaults."""
    mtype = m["type"]
    if mtype not in TYPE_MAP:
        raise ValueError(f"Unsupported monitor type '{mtype}' for '{m.get('name')}'")

    out: dict = {"name": m["name"], "type": TYPE_MAP[mtype]}
    for f in COMMON_FIELDS:
        out[f] = m.get(f, defaults.get(f))
    for f in TYPE_FIELDS[mtype]:
        if f == "docker_host":
            host_name = m["docker_host"]
            if host_name not in docker_hosts:
                raise ValueError(f"Monitor '{m['name']}' references unknown docker_host '{host_name}'")
            out["docker_host"] = docker_hosts[host_name]
        elif f in m:
            out[f] = m[f]

    desc = (m.get("description") or "").strip()
    out["description"] = f"{desc}\n{MANAGED_TAG}".strip()
    return out


def needs_update(current: dict, desired: dict) -> list[str]:
    """Return the list of managed fields that differ between current and desired."""
    drifted = []
    for key, want in desired.items():
        if key == "type":
            # Kuma returns type as a string like "http"; compare case-insensitively.
            have = str(current.get("type", "")).lower()
            if have != str(want.value if isinstance(want, MonitorType) else want).lower():
                drifted.append(key)
            continue
        if current.get(key) != want:
            drifted.append(key)
    return drifted


def sync(api: UptimeKumaApi, cfg: dict, dry_run: bool, prune: bool) -> None:
    defaults = cfg["defaults"]
    docker_hosts = ensure_docker_hosts(api, cfg["docker_hosts"], dry_run)

    existing = {m["name"]: m for m in api.get_monitors()}
    desired_names = {m["name"] for m in cfg["monitors"]}

    created = updated = unchanged = 0
    for m in cfg["monitors"]:
        want = desired_monitor(m, defaults, docker_hosts)
        name = want["name"]
        if name not in existing:
            log.info("CREATE  %-28s (%s)", name, m["type"])
            created += 1
            if not dry_run:
                result = api.add_monitor(**want)
                if m["type"] == "push":
                    _print_push_url(api, result["monitorID"], name)
            continue

        current = existing[name]
        drifted = needs_update(current, want)
        if drifted:
            log.info("UPDATE  %-28s changed: %s", name, ", ".join(drifted))
            updated += 1
            if not dry_run:
                api.edit_monitor(current["id"], **want)
        else:
            unchanged += 1
            log.debug("OK      %s", name)

    pruned = 0
    if prune:
        for name, current in existing.items():
            if name in desired_names:
                continue
            if MANAGED_TAG not in (current.get("description") or ""):
                log.debug("SKIP prune (unmanaged) %s", name)
                continue
            log.info("DELETE  %-28s (managed, no longer in file)", name)
            pruned += 1
            if not dry_run:
                api.delete_monitor(current["id"])

    verb = "Would" if dry_run else "Did"
    log.info("%s create=%d update=%d prune=%d (unchanged=%d)",
             verb, created, updated, pruned, unchanged)


def _print_push_url(api: UptimeKumaApi, monitor_id: int, name: str) -> None:
    """Print the heartbeat URL for a push monitor (for healthchecks.io etc.)."""
    try:
        mon = api.get_monitor(monitor_id)
        token = mon.get("pushToken")
        if token:
            log.info("PUSH URL for '%s': <kuma-base-url>/api/push/%s?status=up&msg=OK", name, token)
    except Exception:
        log.warning("Created push monitor '%s' but could not read its push token; "
                    "grab it from the UI.", name)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Reconcile Uptime Kuma monitors from monitors.yaml")
    parser.add_argument("--dry-run", action="store_true", help="show changes without applying")
    parser.add_argument("--prune", action="store_true",
                        help="delete managed monitors no longer in the file")
    parser.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "monitors.yaml"))
    args = parser.parse_args()

    kuma_url = env("KUMA_URL").rstrip("/")
    username = env("KUMA_USERNAME")
    password = env("KUMA_PASSWORD")

    cfg = load_config(args.config)

    api = UptimeKumaApi(kuma_url)
    try:
        api.login(username, password)
        sync(api, cfg, dry_run=args.dry_run, prune=args.prune)
    finally:
        api.disconnect()


if __name__ == "__main__":
    main()
