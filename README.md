# ERP ↔ MES ↔ PLC Demo

Batch process integration over MQTT (ERP ↔ MES ↔ PLC), built on EMQX and
EMQX Neuron.

The shop-floor scenario is a CNC machining cell — two PLCs running a
multi-phase part program (`load → rough → finish → unload`) and streaming
generic CNC telemetry (spindle RPM, feed rate, coolant temperature, torque,
power). Alongside them, a third neuron talks **BACnet/IP** to a simulated
facility HVAC unit, showing the same Neuron + EMQX stack ingesting both
shop-floor OPC UA and brownfield building-automation BACnet side-by-side.

![Interactive demo with device controls, Neuron gateways, EMQX and live MQTT traffic](docs/images/demo.jpg)

## Quick start

Install Docker with Docker Compose and [uv](https://docs.astral.sh/uv/).
The setup helpers also require `make`, `python3` and `openssl` on your PATH.
Run these commands from the repository root:

```bash
make up           # generate local credentials, then build and start the stack
make provision    # configure gateways, viewers, database grants and retention
```

After provisioning, open <http://localhost:8080> and wait for **Device data live**:
both CNCs and the HVAC unit must have fresh telemetry. Provisioning retries
while Neuron starts; first boot can take about 30 seconds. `make help` lists
the available targets.

App images are pinned to EMQX 6.3.2, EMQX Neuron 4.0.0, Grafana 13.2.2, and
TimescaleDB 2.30.1 on PostgreSQL 17.

## Live demo

The home page combines device controls, ERP batch orders and live MQTT traffic.
Equipment is simulated; OPC UA/BACnet acquisition, MQTT, MES execution and
database storage use the running services. All services, including the
application tier drawn above EMQX, run locally in Docker.

**Tools** opens the product UIs. **Namespace** opens the MQTT topic tree.
Direct links, once the stack is running:

- **Namespace** — <http://localhost:8080/uns>
- **MES** — <http://localhost:8090> (read-only batch execution view)
- **Grafana** — <http://localhost:3000/d/production-overview> (machining) / <http://localhost:3000/d/facility-hvac> (HVAC)
- **EMQX dashboard** — <http://localhost:18083> (viewer / demo-viewer)
- **EMQX Neuron** — CNC 01 <http://localhost:8085>, CNC 02 <http://localhost:8086>, HVAC <http://localhost:8087> (viewer / demo-viewer)

The base Compose stack binds published ports to `127.0.0.1`. EMQX, Neuron
and Grafana administrator passwords and MQTT service passwords are generated
in the gitignored `.env`; broker files and the MQTT signing key are stored
in the gitignored `.runtime/`. `make security-init` preserves existing secrets.
The shared `viewer / demo-viewer` accounts are read-only. Grafana uses
anonymous Viewer access and a database role restricted to reading the three
demo history tables.

### Testing with separate browsers

1. Open the home page in two browsers, or a regular window and a private
   window. Tabs in the same browser profile share one visitor session.
2. In browser A, **Claim · 30s** on CNC 01, then **Set RPM**. Watch the
   `cmd/req`, gateway `cmd/ack`, and changed spindle reading. The spindle
   setting is bounded to 0–6,000 rpm and only applies outside MES batches.
3. In browser B, **Join queue** on CNC 01. Its controls remain disabled
   until A releases or expires and the device defaults are restored.
   **Leave queue** lets B cancel and choose another device.
4. In browser A, **Release** CNC 01. Watch B receive control with the
   spindle stopped, then release it in B as well.
5. Claim HVAC to change temperature (18–28 °C), fan speed (0–100%), mode
   and power. Click **Apply** to send the settings through Neuron over
   BACnet/IP, then **Release** before trying an ERP order.
6. Use **Send batch** on ERP to queue one part on the selected CNC.
   MES first acknowledges the reservation, then runs the selected recipe.
   The submitting browser can Hold, Resume, inject/clear a fault or End batch
   from that CNC's card. Other browsers cannot change its settings.
7. Sign into EMQX and Neuron with `viewer / demo-viewer`: reads work, writes
   are rejected. Public visitors cannot write through the MES API or publish
   directly to MQTT.

### Limits and access

Each device allows one active reservation and at most three waiting requests.
Each browser may have only one active or waiting request across all devices.
Manual claims last 30 seconds; abandoned manual claims and waiting requests
expire after 20 seconds without polling. Waiting requests also expire after
10 minutes even if the browser stays open. ERP reservations last through batch
completion, with a 90-second cap. A batch continues if its browser closes.
Expired, released or restarted claims restore device defaults before handoff;
CNC spindle deadlines are also enforced locally by the PLC simulator.

Claim and ERP submissions are limited to 6 requests/minute per browser,
15/minute per IP and 40/minute globally. State-changing device commands are
limited to 8 per 5 seconds per visitor and one unconfirmed setting change
per device. Status snapshots need no claim, but are limited to 6 per 10 seconds
per visitor and 90/minute
globally. Full queues and rate limits return HTTP 429 with `Retry-After`.

HTTP controls require a CSRF token; writes also require the owner's unexpired
lease. Batch controls validate the current batch and phase. Retried queue
submission IDs do not create another request. Cookie limits are not proof of
unique human identity: clearing cookies or changing browsers creates another
visitor, with IP and global limits still applying.

Browser MQTT connections use five-minute RS256 JWTs bound to client ID and
username. Only the operator holds the private signing key. Broker ACLs allow
subscriptions to demo telemetry, commands and events; publishing and gateway
file-management topics are denied. Internal services use separate credentials
and topic ACLs. The namespace tree and traffic panes are real MQTT subscribers.

## Layout

<details>
<summary>Architecture diagram</summary>

![Architecture](services/operator/static/architecture.svg)

</details>

```
docker-compose.yml             EMQX + 2 PLCs + 1 HVAC + 3 neurons + MES + operator + storage
services/operator/             Flask: interactive controls, device claims, MQTT traffic and namespace tree
services/mes-sim/              Python: state-machine MES, web UI for batches in flight
services/plc-sim/              Python: OPC UA machining-cell PLC, command-driven
services/hvac-sim/             Python: BACnet/IP rooftop AHU simulator (bacpypes3)
emqx/base.hocon                rule engine + Timescale sinks (plc + hvac)
timescale/init.sql             plc_telemetry + hvac_telemetry hypertables + batches table
grafana/                       provisioned datasource + production-overview + facility-hvac dashboards
tools/provision_neuron.py      REST-API neuron provisioning (OPC UA on neuron-1/2, BACnet on neuron-3)
tools/provision_security.py    product viewers, Grafana database grants and retention
tools/init_security.py         local credentials, MQTT signing key and broker ACLs
```

## Operating the demo

### Commands and live traffic

ERP offers **Full program** (`program-a`: load → rough → finish → unload)
and **Quick program** (`program-quick`: load → rough → unload). An ERP order
joins the selected CNC's queue and reserves it for the batch. MES cannot
allocate a machine that has a manual claim, and manual settings cannot
alter a reserved batch. HVAC is independent of MES production allocation.

The right side has two independent displays:

- **Commands & events**: orders, reservations, device writes, gateway
  acknowledgements, lifecycle events and batch records. The last 100 messages
  are kept; unchanged reservation heartbeats and duplicate retained states
  on reconnect are suppressed. Rows name the device and summarize writes.
- **Telemetry**: bundled readings, displayed at most once per device per
  second, with a 60-message history and the actual incoming message rate.

Each pane lists its actual MQTT subscription filters and has Pause/Clear.
Use each row's **JSON ›** disclosure to inspect its full topic and payload.
Pausing freezes that display and shows **Paused**; it does not pause devices.
Retained messages say **retained · on connect**. A successful gateway
acknowledgement means the write was accepted; the card reports success only
after the requested values appear in fresh telemetry.

**Status** publishes a request and a response containing the latest observed
telemetry, with its timestamp and freshness. This is a cached snapshot; it
does not trigger a new OPC UA or BACnet read. The reply also appears on the
device card when its MQTT viewer is paused or active in another tab.

**Device data live** describes backend device monitoring. Each MQTT pane separately
shows its browser subscription state. Tabs in the same browser profile share
one MQTT viewer for the home page and one for the namespace tree. A displaced
viewer shows **Other tab**; **Watch here** moves the feed back to that tab.

Blue paths carry telemetry/events; purple paths carry orders/commands. Highlighted
paths reflect recently observed MQTT messages, not exact network transit times. The
larger device dots show telemetry freshness; claim buttons show control ownership
and the remaining turn time. An idle machine can have fresh readings;
a connected broker alone does not prove that a device is healthy.

### Release and recovery

**Release** ends your manual claim. **End batch** cancels your own ERP order
through MQTT → MES → Neuron → OPC UA. Both wait for confirmed device recovery
before the next visitor receives control. CNC defaults are spindle stopped;
HVAC defaults are 22 °C, 50% fan, Auto mode and Power On. History is preserved.

Claims persist in SQLite. After an operator restart, active claims recover
before accepting further writes; waiting requests are cancelled. If a gateway
is offline, recovery waits for its write acknowledgement and fresh default
values. Restore connectivity to resume that device's queue.

For operator recovery, `make booth-reset` uses the private local administrator
key to recover active claims while preserving database history.

| Symptom | What to check |
| --- | --- |
| Source data stays stale | Run `docker compose ps`, then `make provision`. Inspect the relevant Neuron driver and tags. |
| Device says Restoring defaults | Restore its gateway/MQTT connection; handoff waits for acknowledgement and fresh baseline values. |
| MQTT pane is disconnected | Check the WebSocket listener at `ws://localhost:8083/mqtt`. The browser renews its read-only token automatically. |
| Controls are disabled | Check the device's claim/queue label; only its current owner may write, and batches reserve their CNC. |

Useful diagnostics:

```bash
docker compose ps
docker compose logs --tail=100 operator mes plc-1 plc-2 hvac-1
curl -i http://localhost:8080/readyz
```

`/readyz` returns 200 only when MQTT and all three device sources are fresh;
`/healthz` checks the operator HTTP service only.

## Configuration

Use `.env` for shared configuration. `SHOWCASE_TITLE` changes the browser-tab
title. `ENTERPRISE`, `SITE` and `LINE` set the MQTT namespace, with defaults
`acme-mfg`, `plant-1` and `line-1`. CNCs use the fixed area `machining`;
HVAC uses `utilities/compressor-room` to match the broker rules and controls.

The provisioners read `.env`, with shell values taking precedence. The Neuron
provisioner accepts `NEURON_API_1` through `NEURON_API_3` as `host:port` values
and `BACNET_HOST` as the HVAC device's numeric IP.

`CLAIM_SECONDS` (30), `BATCH_SECONDS` (90) and `DEVICE_QUEUE_SIZE` (3) can be
set in `.env`, then applied with `make up`. Keep manual claims at or below
60 seconds to match the PLC's independent deadline guard. Run one operator
process: the supplied server uses one scheduler and eight HTTP threads.

MES has two fixed workers and a three-order backlog, validates order expiry,
persists deduplication, and marks interrupted orders failed after restart. Batch IDs
are UUIDs; in-memory batch history is capped at 100. Retained messages expire
after a day, telemetry after seven days and database batch records after 30 days.

### Optional HTTPS hosting

The hosted profile adds Caddy, HTTPS cookies and a shared entrance password
to the same restrictions. Point a dedicated demo hostname and its `mes`,
`grafana`, `emqx`, `neuron-1`, `neuron-2` and `neuron-3` subdomains at the host.
Allow inbound ports 80 and 443, then run:

```bash
uv run tools/booth_credentials.py factory.example.com  # use your own hostname
make booth-up
make provision
```

The credential helper prompts for the entrance password and creates a private
`.env.booth`. Local testing uses the base Compose stack and needs no domain
or HTTPS.

## Checking the demo

```bash
make check               # ownership, limits, expiry, recovery and MES allocation
make check-ui            # UI state/connection regressions; Node 18+ (no npm dependencies)
make check-restrictions  # running, provisioned stack; start with no active claims
```

The live check uses six independent cookie jars, actual MQTT connections,
product APIs and a database connection. It checks forbidden writes, queue
capacity, copied leases, real OPC UA/BACnet setting changes, default recovery,
FIFO handoff, manual expiry and a complete ERP batch on a different CNC.
It restores its own devices afterward. For manual verification, repeat the
browser steps above and inspect the MQTT request/acknowledgement/value chain.

## Topics

```
acme-mfg/<site>/orders/<id>                                              # ERP → MES
acme-mfg/<site>/orders/<id>/status                                       # MES → ERP (retained)
acme-mfg/<site>/machining/<line>/<plc>/state                             # PLC (retained, synthesized)
acme-mfg/<site>/machining/<line>/<plc>/telemetry                         # PLC via Neuron (OPC UA)
acme-mfg/<site>/machining/<line>/<plc>/state-events                      # PLC transitions via Neuron
acme-mfg/<site>/machining/<line>/<plc>/batch/<bid>/phase/<p>/command     # MES → PLC
acme-mfg/<site>/batches/<bid>/record                                     # MES (retained)
acme-mfg/<site>/utilities/compressor-room/hvac-1/telemetry               # HVAC via Neuron (BACnet/IP)
acme-mfg/<site>/<area>/<line>/<device>/cmd/req                           # Scoped Neuron write
acme-mfg/<site>/<area>/<line>/<device>/cmd/ack                           # Gateway acknowledgement
acme-mfg/<site>/<area>/<line>/<device>/control/request                   # Visitor command / status request
acme-mfg/<site>/<area>/<line>/<device>/control/response                  # Observed status snapshot
acme-mfg/<site>/machining/<line>/<plc>/control/reservation               # Retained MES reservation
acme-mfg/<site>/machining/<line>/<plc>/control/reservation/ack           # MES reservation acknowledgement
```

`acme-mfg` is the default enterprise placeholder; set `ENTERPRISE` to change it.

## Stopping or rebuilding from scratch

For the local stack, `make down` stops the services and preserves their named volumes. Use
`make up` to start them again.

To erase an existing local stack, the following commands **delete its named
volumes**, including database history, claims, Grafana state and current
Neuron configuration, then recreate and provision it. The `.env` and
`.runtime/` files remain. Use **Release** or **End batch** for ordinary turns.

```bash
make reset
make provision
```
