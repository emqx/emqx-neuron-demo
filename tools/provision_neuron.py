# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Configure each EMQX Neuron instance via REST API.

Two south OPC UA drivers per neuron, both pointing at the same PLC sim:

  src-opcua          mode 1 (Update by Time, 1 Hz polled)
                     telemetry + writable command/fault tags
                     subscribed to .../telemetry

  src-opcua-events   mode 2 (Update on Change)
                     lifecycle tags (phase_state, current_batch_id, ...)
                     subscribed to .../state-events

This separation means continuous telemetry for Grafana while phase-state
events fire only on actual transitions — the synth rule on EMQX gets exactly
one event per real state change, no dedup required.

Idempotent: nodes/groups/subscriptions that already exist are left alone.

Reprovisioning: this script does not delete nodes — EMQX Neuron's embedded
daemon reloads on DELETE and returns transient HTML errors mid-reload. To
start clean, nuke the neuron volumes (`make reset`).
"""
from __future__ import annotations

import os
import sys
import time

import httpx


ENTERPRISE = os.getenv("ENTERPRISE", "acme-mfg")
SITE       = os.getenv("SITE", "plant-1")
AREA       = os.getenv("AREA", "machining")
LINE       = os.getenv("LINE", "line-1")


NEURONS = [
    {"neuron": f"localhost:{8084 + i}", "plc_host": f"plc-{i}",
     "plc_id": f"plc-{i}", "axis_id": "axis-01",
     "enterprise": ENTERPRISE, "site": SITE, "area": AREA, "line": LINE}
    for i in range(1, 4)
]

DRIVER_TM     = "src-opcua"          # telemetry, mode 1
DRIVER_EV     = "src-opcua-events"   # events,    mode 2
APP           = "uns-mqtt"
GROUP_TM      = "plc"
GROUP_EV      = "events"
GROUP_INTERVAL_MS = 1000


# (name, ns!nid, neuron type id, attribute) — ns!nid must match plc.py
# explicit NodeIds. Type: 10 = DOUBLE, 13 = STRING. Attr: 1=read 2=write 3=rw.
TELEMETRY_TAGS = [
    # Read-only telemetry.
    ("spindle_rpm",      "2!2",  10, 1),
    ("feed_mm_min",      "2!3",  10, 1),
    ("coolant_temp_c",   "2!4",  10, 1),
    ("torque_nm",        "2!5",  10, 1),
    ("power_kw",         "2!6",  10, 1),
    ("state",            "2!7",  13, 1),
    ("plc_id",           "2!8",  13, 1),
    ("axis_id",          "2!9",  13, 1),
    # Phase lifecycle, also polled here so the operator console can derive
    # PLC state at 1 Hz from telemetry (true even when nothing changes).
    ("phase_state",      "2!10", 13, 1),
    ("current_batch_id", "2!11", 13, 1),
    ("current_phase",    "2!12", 13, 1),
    ("state_reason",     "2!13", 13, 1),
    # Writable command/fault tags. PLC polls these locally; their actual
    # poll mode by Neuron is irrelevant since they're written, not read.
    ("command_action",   "2!14", 13, 3),
    ("command_phase",    "2!15", 13, 3),
    ("command_batch_id", "2!16", 13, 3),
    ("command_seq",      "2!17", 13, 3),
    ("fault_action",     "2!18", 13, 3),
    ("fault_type",       "2!19", 13, 3),
    ("fault_seq",        "2!20", 13, 3),
    # Composite event tag (read-only here; PLC writes the JSON snapshot).
    ("phase_event_json", "2!21", 13, 1),
]

# Event tag — single composite tag carrying a JSON snapshot of each PLC
# transition. Attribute = 4 (Subscribe) tells Neuron to use OPC UA's native
# monitored-item mechanism so the server pushes one notification per write.
# Combined with update_mode=2 on this driver, that gives one MQTT event per
# real PLC transition — no 1 Hz polling repeats and no MES-side dedup.
EVENT_TAGS = [
    ("phase_event_json", "2!21", 13, 4),
]


def login(base: str, user: str = "admin", password: str = "0000") -> str:
    deadline = time.time() + 60
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.post(f"http://{base}/api/login",
                           json={"name": user, "pass": password}, timeout=5)
            r.raise_for_status()
            return r.json()["token"]
        except (httpx.HTTPError, KeyError) as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"login {base} failed: {last_err}")


def call(client: httpx.Client, method: str, path: str, **kw) -> dict:
    # EMQX Neuron reverse-proxies /api/neuron/* to its embedded neuron daemon.
    # After a DELETE the daemon briefly reloads and returns a generic HTML
    # 405/502/503/504. Retry transient upstream errors.
    deadline = time.time() + 30
    delay = 0.5
    while True:
        r = client.request(method, path, **kw)
        ctype = r.headers.get("content-type", "")
        transient = r.status_code >= 400 and "html" in ctype.lower()
        if not transient or time.time() > deadline:
            break
        time.sleep(delay)
        delay = min(delay * 2, 4)
    if r.status_code >= 400 and "json" not in r.headers.get("content-type", ""):
        raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text}")
    try:
        return r.json()
    except Exception:
        return {}


def maybe_exists(body: dict, what: str) -> None:
    err = body.get("error")
    if err not in (0, None):
        print(f"    note: {what} returned error={err} (likely exists, continuing)")


def setup_driver(c: httpx.Client, name: str, opcua_url: str, update_mode: int) -> None:
    body = call(c, "POST", "/api/neuron/node",
                json={"name": name, "plugin": "OPC UA"})
    maybe_exists(body, f"create driver {name}")
    call(c, "POST", "/api/neuron/node/setting", json={
        "node": name,
        "params": {
            "name": name, "plugin": "OPC UA",
            "url": opcua_url,
            "username": "", "password": "",
            "cert": "", "key": "",
            "publish-interval": 500,
            "security_mode": 1,
            "update_mode": update_mode,
        },
    })


def setup_group(c: httpx.Client, driver: str, group: str, tags: list) -> None:
    # One tag per POST. Two EMQX Neuron quirks make bulk inserts unreliable:
    #   1. Large bulk POSTs silently truncate — only the first N tags land,
    #      the rest are dropped (response `index` reports the count, but
    #      `error` stays 0).
    #   2. Within a single POST, the first duplicate-name error (2202) aborts
    #      processing — every subsequent tag in that POST is skipped.
    # Posting one tag at a time sidesteps both; calls are idempotent so a
    # duplicate on one tag doesn't block the others.
    for (name, addr, type_, attr) in tags:
        body = call(c, "POST", "/api/neuron/tags", json={
            "node": driver, "group": group,
            "tags": [{"name": name, "address": addr, "attribute": attr, "type": type_}],
        })
        maybe_exists(body, f"create tag {driver}/{group}/{name}")
    call(c, "PUT", "/api/neuron/group", json={
        "node": driver, "group": group, "interval": GROUP_INTERVAL_MS,
    })


def provision(n: dict) -> None:
    base = n["neuron"]
    token = login(base)
    print(f"  login ok: {base}")

    with httpx.Client(
        base_url=f"http://{base}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ) as c:
        opcua_url = f"opc.tcp://{n['plc_host']}:4840/freeopcua/server/"
        topic_tm = (f"{n['enterprise']}/{n['site']}/{n['area']}/"
                    f"{n['line']}/{n['plc_id']}/telemetry")
        topic_ev = (f"{n['enterprise']}/{n['site']}/{n['area']}/"
                    f"{n['line']}/{n['plc_id']}/state-events")

        # Two OPC UA drivers, same endpoint, different update modes.
        setup_driver(c, DRIVER_TM, opcua_url, update_mode=1)
        setup_group(c, DRIVER_TM, GROUP_TM, TELEMETRY_TAGS)

        setup_driver(c, DRIVER_EV, opcua_url, update_mode=2)
        setup_group(c, DRIVER_EV, GROUP_EV, EVENT_TAGS)

        # MQTT north app.
        body = call(c, "POST", "/api/neuron/node",
                    json={"name": APP, "plugin": "MQTT"})
        maybe_exists(body, f"create app {APP}")
        call(c, "POST", "/api/neuron/node/setting", json={
            "node": APP,
            "params": {
                "name": APP, "plugin": "MQTT",
                "client-id": f"neuron-{n['plc_id']}",
                "host": "emqx", "port": 1883,
                "username": "", "password": "",
                "ssl": False, "qos": 1, "version": 5, "format": 0,
                "cache": False, "cache-mem-size": 0, "cache-disk-size": 0,
                "cache-sync-interval": 100,
                "write-req-topic":  f"/neuron/{n['plc_id']}/write/req",
                "write-resp-topic": f"/neuron/{n['plc_id']}/write/resp",
            },
        })

        # Subscribe MQTT app to both groups.
        for driver, group, topic in (
            (DRIVER_TM, GROUP_TM, topic_tm),
            (DRIVER_EV, GROUP_EV, topic_ev),
        ):
            body = call(c, "POST", "/api/neuron/subscribe", json={
                "app": APP, "driver": driver, "group": group,
                "params": {"topic": topic},
            })
            maybe_exists(body, f"subscribe {driver}/{group}")

        print(f"  ✓ {n['plc_id']:10s} → {topic_tm}")
        print(f"                 {topic_ev}")


def main() -> int:
    for n in NEURONS:
        print(f"==> {n['plc_id']} via {n['neuron']}")
        try:
            provision(n)
        except Exception as e:
            print(f"  FAIL: {e}", file=sys.stderr)
            return 1
    print("\nDone. Topic prefixes:")
    print(f"  {ENTERPRISE}/{SITE}/{AREA}/{LINE}/<plc>/telemetry")
    print(f"  {ENTERPRISE}/{SITE}/{AREA}/{LINE}/<plc>/state-events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
