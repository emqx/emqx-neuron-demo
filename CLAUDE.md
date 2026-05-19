# Project notes for Claude

Batch process manufacturing demo (themed as a CNC machining cell, but
domain-neutral underneath). ERP ↔ MES ↔ PLC over MQTT, built on EMQX +
EMQX Neuron.

`README.md` covers user-facing setup. This file captures things that bit
prior sessions and the mental model for fast debugging.

## Compose stack

- `docker-compose.yml` — full stack (3 PLCs, 3 neurons, EMQX, Postgres,
  Grafana, MES, operator). Project name `erp-mes-plc-demo`.

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

Commands flow MES → MQTT phase-cmd topic → EMQX rule → `/neuron/<plc>/write/req`
→ Neuron OPC UA write → PLC polls its own command_seq tag and reacts.
Same path for fault inject/clear via `_demo/fault/<plc>`.

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
- **`nth()` indices on the new 6-segment telemetry topic**:
  nth(1)=enterprise, nth(2)=site, nth(3)=area, nth(4)=line, nth(5)=plc,
  nth(6)=`telemetry`. The phase-command topic is 10 segments; the plc
  segment is still nth(5). The fault topic stays `_demo/fault/<plc>` so
  plc is nth(3) there.

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
- **3 PLCs / 3 neurons** by design — enough to make the fleet feel real
  without being overkill for a live demo.
