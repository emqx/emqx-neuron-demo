# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Configure each EMQX Neuron instance via REST API.

neuron-1 / neuron-2 are wired to OPC UA PLCs (plc-1, plc-2) — two drivers
per neuron at the same endpoint:

  src-opcua          mode 1 (Update by Time, 1 Hz polled)
                     telemetry + writable command/fault tags
                     subscribed to .../telemetry

  src-opcua-events   mode 2 (Update on Change)
                     lifecycle tags (phase_state, current_batch_id, ...)
                     subscribed to .../state-events

neuron-3 is wired to the BACnet/IP HVAC simulator (hvac-1) with one driver,
demonstrating Neuron's multi-protocol reach. BACnet objects → MQTT JSON on
the utilities topic.

Idempotent: nodes/groups/subscriptions that already exist are left alone.
EMQX Neuron's embedded daemon reloads on DELETE and returns transient HTML
errors mid-reload; the call helper retries those.
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
HVAC_AREA  = os.getenv("HVAC_AREA", "utilities")
HVAC_LINE  = os.getenv("HVAC_LINE", "compressor-room")


DRIVER_TM       = "src-opcua"          # OPC UA telemetry, mode 1
DRIVER_EV       = "src-opcua-events"   # OPC UA events,    mode 2
DRIVER_BACNET   = "src-bacnet"         # BACnet/IP HVAC
APP             = "uns-mqtt"
GROUP_TM        = "plc"
GROUP_EV        = "events"
GROUP_BACNET    = "hvac"
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
EVENT_TAGS = [
    ("phase_event_json", "2!21", 13, 4),
]


# BACnet tag map. Address format: AREA<index>; default property is
# Present_Value. Type IDs: 9=FLOAT (AI/AV float), 11=BIT (BV), 2=UINT8 (MSV).
# Attr: 1=read 3=read/write. Address index must match hvac.py object map.
HVAC_TAGS = [
    ("supply_air_temp_c",    "AI0",  9, 1),
    ("return_air_temp_c",    "AI1",  9, 1),
    ("outside_air_temp_c",   "AI2",  9, 1),
    ("chilled_water_temp_c", "AI3",  9, 1),
    ("fan_kw",               "AI4",  9, 1),
    ("filter_dp_pa",         "AI5",  9, 1),
    ("unit_running",         "BV0", 11, 1),
    ("fault_active",         "BV1", 11, 1),
    ("mode_actual",          "MSV0", 2, 1),
    ("temp_setpoint_c",      "AV0",  9, 3),
    ("fan_speed_pct",        "AV1",  9, 3),
    ("mode_cmd",             "MSV1", 2, 3),
    ("enable_cmd",           "BV2", 11, 3),
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


def setup_opcua_driver(c: httpx.Client, name: str, opcua_url: str,
                      update_mode: int) -> None:
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


def setup_bacnet_driver(c: httpx.Client, name: str, host_ip: str, port: int,
                       device_id: int, device_network: int = 0) -> None:
    body = call(c, "POST", "/api/neuron/node",
                json={"name": name, "plugin": "BACnet/IP"})
    maybe_exists(body, f"create driver {name}")
    # Schema is strict: host must be a numeric IP (regex-enforced), and the
    # driver expects `src_port`, `bbmd`, `device_network`, `device_id` —
    # not the more obvious `target_device_*` names from the prose docs.
    #   - src_port MUST be 0 (ephemeral): the driver binds a fresh source
    #     socket per read, so a fixed src_port collides with itself after the
    #     first read.
    #   - device_network MUST be 0 for a directly-reachable (local-network)
    #     device. The schema UI enforces min=1, but the REST API accepts 0,
    #     and 0 is correct: with 1, Neuron treats hvac-1 as sitting on remote
    #     BACnet network 1 reachable via a router, the link never establishes
    #     (state shows link:0, rtt:9999) even though unicast reads still
    #     happen to work. With 0 the link comes up clean (link:1).
    call(c, "POST", "/api/neuron/node/setting", json={
        "node": name,
        "params": {
            "src_port": 0,
            "host": host_ip,
            "port": port,
            "bbmd": 0,
            "device_network": device_network,
            "device_id": device_id,
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


def setup_mqtt_app(c: httpx.Client, client_id: str, write_req_topic: str,
                   write_resp_topic: str) -> None:
    body = call(c, "POST", "/api/neuron/node",
                json={"name": APP, "plugin": "MQTT"})
    maybe_exists(body, f"create app {APP}")
    call(c, "POST", "/api/neuron/node/setting", json={
        "node": APP,
        "params": {
            "name": APP, "plugin": "MQTT",
            "client-id": client_id,
            "host": "emqx", "port": 1883,
            "username": "", "password": "",
            "ssl": False, "qos": 1, "version": 5, "format": 0,
            "cache": False, "cache-mem-size": 0, "cache-disk-size": 0,
            "cache-sync-interval": 100,
            "write-req-topic":  write_req_topic,
            "write-resp-topic": write_resp_topic,
        },
    })


def provision_opcua(base: str, plc_id: str) -> None:
    token = login(base)
    print(f"  login ok: {base}")
    with httpx.Client(
        base_url=f"http://{base}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ) as c:
        opcua_url = f"opc.tcp://{plc_id}:4840/freeopcua/server/"
        topic_tm = f"{ENTERPRISE}/{SITE}/{AREA}/{LINE}/{plc_id}/telemetry"
        topic_ev = f"{ENTERPRISE}/{SITE}/{AREA}/{LINE}/{plc_id}/state-events"
        write_req  = f"{ENTERPRISE}/{SITE}/{AREA}/{LINE}/{plc_id}/cmd/req"
        write_resp = f"{ENTERPRISE}/{SITE}/{AREA}/{LINE}/{plc_id}/cmd/ack"

        setup_opcua_driver(c, DRIVER_TM, opcua_url, update_mode=1)
        setup_group(c, DRIVER_TM, GROUP_TM, TELEMETRY_TAGS)
        setup_opcua_driver(c, DRIVER_EV, opcua_url, update_mode=2)
        setup_group(c, DRIVER_EV, GROUP_EV, EVENT_TAGS)

        setup_mqtt_app(c, f"neuron-{plc_id}", write_req, write_resp)

        for driver, group, topic in (
            (DRIVER_TM, GROUP_TM, topic_tm),
            (DRIVER_EV, GROUP_EV, topic_ev),
        ):
            body = call(c, "POST", "/api/neuron/subscribe", json={
                "app": APP, "driver": driver, "group": group,
                "params": {"topic": topic},
            })
            maybe_exists(body, f"subscribe {driver}/{group}")

        print(f"  ✓ {plc_id:10s} → {topic_tm}")
        print(f"                 {topic_ev}")


def provision_bacnet(base: str, name: str, host_ip: str, device_id: int) -> None:
    token = login(base)
    print(f"  login ok: {base}")
    with httpx.Client(
        base_url=f"http://{base}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ) as c:
        topic_tm   = f"{ENTERPRISE}/{SITE}/{HVAC_AREA}/{HVAC_LINE}/{name}/telemetry"
        write_req  = f"{ENTERPRISE}/{SITE}/{HVAC_AREA}/{HVAC_LINE}/{name}/cmd/req"
        write_resp = f"{ENTERPRISE}/{SITE}/{HVAC_AREA}/{HVAC_LINE}/{name}/cmd/ack"

        setup_bacnet_driver(c, DRIVER_BACNET, host_ip, 47808, device_id)
        setup_group(c, DRIVER_BACNET, GROUP_BACNET, HVAC_TAGS)

        setup_mqtt_app(c, f"neuron-{name}", write_req, write_resp)

        body = call(c, "POST", "/api/neuron/subscribe", json={
            "app": APP, "driver": DRIVER_BACNET, "group": GROUP_BACNET,
            "params": {"topic": topic_tm},
        })
        maybe_exists(body, f"subscribe {DRIVER_BACNET}/{GROUP_BACNET}")

        print(f"  ✓ {name:10s} → {topic_tm}")


def main() -> int:
    targets: list[tuple[str, str, dict]] = [
        ("opcua",  f"localhost:8085", {"plc_id": "plc-1"}),
        ("opcua",  f"localhost:8086", {"plc_id": "plc-2"}),
        ("bacnet", f"localhost:8087",
         {"name": "hvac-1", "host_ip": "172.30.0.50", "device_id": 3001}),
    ]
    for kind, base, params in targets:
        label = params.get("plc_id") or params.get("name")
        print(f"==> {label} via {base} [{kind}]")
        try:
            if kind == "opcua":
                provision_opcua(base, params["plc_id"])
            else:
                provision_bacnet(base, params["name"],
                                 params["host_ip"], params["device_id"])
        except Exception as e:
            print(f"  FAIL: {e}", file=sys.stderr)
            return 1
    print("\nDone. Topic prefixes:")
    print(f"  {ENTERPRISE}/{SITE}/{AREA}/{LINE}/<plc>/telemetry        (OPC UA)")
    print(f"  {ENTERPRISE}/{SITE}/{AREA}/{LINE}/<plc>/state-events     (OPC UA)")
    print(f"  {ENTERPRISE}/{SITE}/{HVAC_AREA}/{HVAC_LINE}/hvac-1/telemetry (BACnet/IP)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
