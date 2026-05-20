# Project notes for Claude

Batch process manufacturing demo (themed as a CNC machining cell, but
domain-neutral underneath). ERP ↔ MES ↔ PLC over MQTT, built on EMQX +
EMQX Neuron. neuron-3 talks BACnet/IP to a facility HVAC simulator,
showing the same stack ingesting both OPC UA and BACnet side-by-side.

`README.md` covers user-facing setup. This file captures things that bit
prior sessions and the mental model for fast debugging.

## Compose stack

- `docker-compose.yml` — full stack (2 CNC PLCs over OPC UA, 1 HVAC unit
  over BACnet/IP, 3 neurons, EMQX, Postgres, Grafana, MES, operator).
  Project name `erp-mes-plc-demo`.

## Make targets (`make help` lists them)

`up` / `down` / `reset` / `provision` / `place-order`.

## Pure-OPC-UA control plane

PLC exposes 19 OPC UA tags (NodeIds 2!2..2!20 + 2!21):
telemetry + lifecycle + writable command/fault tags + composite
`phase_event_json` (the events tag).

Two EMQX Neuron drivers per neuron point at the same PLC endpoint:
- `src-opcua` — `update_mode=1`, attr=Read on tags. Polls all 19 tags at
  1 Hz, publishes JSON to `<ent>/<site>/<area>/<line>/<plc>/telemetry`.
- `src-opcua-events` — `update_mode=2`, attr=4 (Subscribe) on
  `phase_event_json` only. Server-pushed on change, publishes to
  `.../state-events`. MES subscribes here directly.

Commands flow MES → MQTT phase-cmd topic → EMQX rule →
`<ent>/<site>/<area>/<line>/<plc>/cmd/req` → Neuron OPC UA write → PLC polls
its own command_seq tag and reacts. Same path for fault inject/clear: the
operator publishes to `<ent>/<site>/<area>/<line>/<plc>/_demo/fault` and the
fault-to-neuron rule reshapes onto the same `cmd/req` topic. Neuron's write
response lands on `cmd/ack` (no consumer today; kept for visibility).

The MQTT→OPC UA write step is **not a EMQX Neuron rule** — it's built into the
MQTT north app. Setting the app's `write-req-topic` / `write-resp-topic`
params (in `tools/provision_neuron.py`) makes the app subscribe to that
topic and treat each incoming JSON message as a write command, routed to
the named driver. Required payload shape:
```json
{"uuid":"<id>","node":"<driver>","tags":[{"tag":"<name>","value":<v>}]}
```
`uuid` is mandatory — without it EMQX Neuron silently drops the request.
Neuron emits `{"uuid":"<id>","error":0}` on `write-resp-topic` after the
OPC UA write completes.

## Topic taxonomy

5-tier ISA-95-ish:
`<enterprise>/<site>/<area>/<line>/<plc>/<suffix>`

Defaults: `acme-mfg/plant-1/machining/line-1/plc-N/...`. Override via
env on mes/operator/plc-N services. Orders and batch records use a
flatter shape (no area/line/plc): `<ent>/<site>/orders/<id>` and
`<ent>/<site>/batches/<bid>/record`.

The chemistry/process domain is meant to be swappable — phase names,
metric tag names, and recipe names are all defined in two places only
(`services/plc-sim/plc.py` PHASES + sample(), and `services/mes-sim/mes.py`
RECIPES) plus the DB column list and the Grafana panels.

## EMQX Neuron gotchas

- **MQTT write-req JSON requires a `uuid` field** for response correlation;
  without it Neuron silently drops the message. Use the EMQX rule's
  `timestamp AS seq` selected field as the uuid.
- **Tag attribute is a bitfield**: `1=Read 2=Write 4=Subscribe`. For real
  on-change OPC UA monitored items you need 4, not 1; `update_mode=2`
  alone with Read attr does not suppress 1 Hz polled emits.
- **`update_mode` is driver-level**, not group-level. Need separate drivers
  for mixed mode-1/mode-2 behavior.
- **`/api/neuron/*` returns transient HTML 4xx/5xx during daemon reloads.**
  Provision script retries on HTML content-type — preserve that pattern in
  any new neuron-API code.
- Default credentials: `admin / 0000` (force-changes via UI but REST keeps
  accepting `0000`).
- **Tag type integer codes (neuronex):** 2=UINT8, 9=FLOAT, 10=DOUBLE,
  11=BIT, 12=BOOL, 13=STRING. Older Neuron 2.0 used different codes —
  match the running image.
- **BACnet/IP plugin name is `"BACnet/IP"`** (slash literal). The prose
  docs and the live REST schema disagree on field names — the schema is
  authoritative. Required params, per `GET /api/neuron/schema?plugin_name=BACnet/IP`:
  `src_port`, `host`, `port`, `bbmd`, `device_network`, `device_id`.
  (Not `target_device_network` / `target_device_id` from the prose docs.)
  - **`src_port` MUST be 0** (ephemeral). The driver binds a fresh socket
    per read; setting src_port=47808 produces `bind 0.0.0.0:47808 error:
    Address already in use(98)` for every read after the first. Logged in
    `/opt/neuronex/software/neuron/logs/src-<driver>.log`, NOT neuron.log.
    MQTT-side symptom: every tag returns `3002` ("plugin not connected").
  - **`host` is regex-validated as a numeric IP**, not a hostname. Pin the
    target with `networks.demo.ipv4_address` in docker-compose.
  - **`device_network` MUST be 0** for a directly-reachable device. The
    schema UI enforces min=1, but the REST API accepts 0. With 1, Neuron
    treats the device as sitting on a remote BACnet network reachable via a
    router; the link never establishes (`link:0 rtt:9999`) even though
    unicast Present_Value reads still happen to work. With 0 the link comes
    up clean (`link:1`).
  - Address format for tags: `AREA<index>[.PROPERTY_ID]` e.g. `AI0`,
    `AV5`, `MSV1`. Default property is `Present_Value`. Supported areas:
    AI/AO/AV/BI/BO/BV/MSI/MSO/MSV/ACC/DEV.
  - BV booleans arrive on MQTT as 0/1 integers. Store as SMALLINT — EMQX's
    rule SQL parser does NOT accept `(x = 1) AS y` syntax for boolean
    coercion, so the cast has to happen on the consumer side instead.
  - Per-driver state via `GET /api/neuron/node/state?node=<name>`:
    `running:3 link:0` = driver up but no successful read — check the
    per-driver log file before chasing connectivity.

## EMQX rule SQL gotchas

- **JSON null becomes the literal string `"null"`.** Wrap nullable columns
  with `NULLIF(${payload.x}, 'null')::timestamptz` (or text). Otherwise
  the action fails with `bad_param` for nullable timestamps.
- **`json_decode(...).field` works in SELECT but not WHERE.** Filter on the
  raw string (`payload.values.phase_event_json <> ''`) and gate at the
  source instead.
- **`${timestamp}` doesn't auto-resolve in republish payload templates.**
  Select it explicitly: `SELECT ..., timestamp AS seq` then use `${seq}`.
- **JSON arrays can't be bound directly to JSONB.** Store the whole
  payload once via `${payload}` and query nested fields with `raw->'x'`.
- **`nth()` indices on the 6-segment telemetry topic**:
  nth(1)=enterprise, nth(2)=site, nth(3)=area, nth(4)=line, nth(5)=plc,
  nth(6)=`telemetry`. All wire topics now share this ISA-95 prefix
  (telemetry, state, state-events, cmd/req, cmd/ack, _demo/fault), so the
  same `nth(1..5)` extraction works in every rule. The phase-command topic
  is 10 segments; plc is still nth(5).
- **Machining and utilities share the topic shape** but differ at nth(3):
  `machining` for CNC PLCs, `utilities` for HVAC. CNC rules now scope to
  `+/+/machining/+/+/...` so HVAC telemetry doesn't accidentally try to
  parse OPC UA-shaped payloads.
- **The UNS view (`operator/templates/uns.html`) subscribes to the exploded
  per-tag leaves `+/+/+/+/+/telemetry/+`, NOT the bundled `.../telemetry`
  blob.** So a source only appears in the UNS if there's an explode rule
  fanning its bundled payload into `.../telemetry/<tag>` leaves. There are
  two: `explode_plc_telemetry` (machining, 5 tags) and
  `explode_hvac_telemetry` (utilities, 11 tags). Add tags to a source →
  also add them to its explode rule or they won't show in the UNS. Each
  rule gates on a known-present field (`plc_id <> ''` / `supply_air_temp_c
  >= -273`) to skip all-error reads that would emit malformed
  `{"value":,...}`.

## Grafana 12 dashboard quirks

- Datasource `type` must be `grafana-postgresql-datasource`, not legacy
  `postgres` — Grafana 12's frontend is strict about this in the panel
  `datasource` field even though the API accepts the alias.
- Panel targets need `editorMode: "code"` and `rawQuery: true` for the
  new postgres plugin to honor `rawSql` instead of trying to build SQL
  from a visual model.
- Stat panel `lastNotNull` calc + `fields: ""` doesn't pick up string
  columns. Use `calcs: ["last"]`, `fields: "/.*/"`.
- Default `minRefreshInterval` is 5 s — refresh values below that get
  silently clamped (warning visible in grafana logs).

## python-opcua

- `add_variable(idx, name, value)` does **not** reliably auto-assign
  sequential `ns!N` NodeIds across many variables. Pin them explicitly:
  `obj.add_variable(f"ns={idx};i={nid}", name, value)`. Provision script
  address mappings depend on these being stable.
- `set_writable()` is required on writable tags; otherwise OPC UA clients
  (Neuron) get a permission error on write.

## Diagnostic recipes

```bash
# MQTT sub from inside the demo network (no host install of mosquitto needed)
docker run --rm --network erp-mes-plc-demo_demo eclipse-mosquitto:latest \
  mosquitto_sub -h emqx -t '<topic>' -v -W 5

# Direct DB query
docker exec timescaledb psql -U demo -d demo -c "..."

# EMQX rules + their SQL
docker exec emqx emqx_ctl rules list
docker exec emqx emqx_ctl rules show <rule_id>

# Live MQTT subscriptions (which neuron is subscribed to which write-req?)
docker exec emqx emqx_ctl subscriptions list

# Neuron tag/group inspection
TOKEN=$(curl -s -X POST http://localhost:8085/api/login \
  -H 'Content-Type: application/json' \
  -d '{"name":"admin","pass":"0000"}' | jq -r .token)
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8085/api/neuron/tags?node=src-opcua&group=plc"
```

When polling for a condition, prefer `Bash run_in_background: true` with
an `until` loop. Don't chain short sleeps; they're blocked by the harness.

## Locked scope (do not re-litigate without explicit user prompt)

- **No Sparkplug B** — JSON-on-MQTT throughout.
- **No genealogy / DPP / multi-site / multi-domain** — out, not phase 2.
- **No customer-specific branding or process-specific claims** — the
  CNC theme is just a relatable example; the demo should keep reading as
  generic batch-process manufacturing. Topic prefix `acme-mfg/` is a
  placeholder enterprise name, override via env.
- **2 CNC PLCs + 1 HVAC + 3 neurons** by design — enough to make the
  fleet feel real, and the HVAC slot demonstrates multi-protocol value
  (BACnet/IP alongside OPC UA) without doubling the moving parts.

## BACnet/HVAC side

- `services/hvac-sim/hvac.py` — bacpypes3 BACnet/IP server. Object map:
  AI0..AI5 sensors, BV0..BV1 status, MSV0 mode_actual, AV0/AV1
  commandable setpoints, MSV1 mode_cmd, BV2 enable_cmd. Must stay in
  sync with `HVAC_TAGS` in `tools/provision_neuron.py` (address +
  type + attr).
- The sim binds with `BACPYPES_DEVICE_ADDRESS=<ip>/16` derived at
  startup via UDP-connect to 8.8.8.8 (single-interface Docker container
  makes the primary IP unambiguous). bacpypes3's ifaddr autodetect also
  works, but UDP-connect is more deterministic.
- Neuron BACnet driver does **not** support an OPC-UA-style Subscribe
  attribute. There's COV in BACnet, but the Neuron driver as shipped
  polls. Acceptable here — HVAC telemetry doesn't need on-change events.
- **Neuron's UI "Scan" is broken for BACnet in neuronex 2.14.1 — device
  independent.** Clicking Scan (or `POST /api/neuron/scan/tags`) starts a
  background probe that floods the device with ReadPropertyMultiple
  (~1100/sec vs the ~13/sec normal group poll), never sets `completed:1`,
  never writes a cache file, and the API perpetually returns
  `error:3000, total:0, tags:[]` — which the UI renders as "large amount
  of data… cached… click Scan again later". Confirmed identical against
  BOTH the bacpypes3 sim AND the upstream **bacnet-stack reference server**
  (built from github.com/bacnet-stack/bacnet-stack), so it is NOT a
  simulator interop issue and swapping sims (bacnet-stack, chipkin) does
  not help. Discovery itself works: with `device_network=0`, Who-Is/I-Am
  and the device `object-list` read both succeed. Use explicit provisioning
  (`tools/provision_neuron.py`) or EDE import (`tools/ede_to_neuron.py`) to
  load tags. If a one-click Scan demo is truly needed, try a newer neuronex
  image — this is a Neuron-side defect, not ours. `services/hvac-sim/hvac.py`
  has an opt-in `HVAC_DEBUG=1` that dumps bacpypes3 APDU logs (how the scan
  was traced; container tcpdump can't capture on Docker Desktop/macOS).
- The HVAC PLC is **not** in MES's allocation pool (`PLCS` in mes.py
  ends at `range(1, 3)`). MES never tries to dispatch batches there.
