# ERP ↔ MES ↔ PLC Demo

Batch process integration over MQTT (ERP ↔ MES ↔ PLC), built on EMQX and
EMQX Neuron.

The shop-floor scenario is a CNC machining cell — two PLCs running a
multi-phase part program (`load → rough → finish → unload`) and streaming
generic CNC telemetry (spindle RPM, feed rate, coolant temperature, torque,
power). Alongside them, a third neuron talks **BACnet/IP** to a simulated
facility HVAC unit, showing the same Neuron + EMQX stack ingesting both
shop-floor OPC UA and brownfield building-automation BACnet side-by-side.

![Architecture](services/operator/static/architecture.svg)

> The same diagram is served on the demo tools page (<http://localhost:8080/tools>).
> Source of truth: `services/operator/static/architecture.svg`.

## Live demo

Open <http://localhost:8080> for the live shop-floor-to-cloud overview:
individual CNC and HVAC devices, their Neuron gateways, the EMQX namespace,
and enterprise applications. Data flows upward; MES commands return to the
addressed CNC. The enterprise/cloud tier represents the application layer;
all services in the local demo run in Docker. Equipment is simulated;
protocol acquisition, MQTT distribution, MES commands and state changes use
the running stack. The guided walkthrough, additional MQTT dashboard,
automatic loop and reset are available on the same page, also at
`/showcase`. **Product screens** opens the individual applications.
All published service ports bind to `127.0.0.1` for local access.

## Layout

```
docker-compose.yml             EMQX + 2 PLCs + 1 HVAC + 3 neurons + MES + operator + storage
services/operator/             Flask: ERP "place order" panel + PLC/order status
services/mes-sim/              Python: state-machine MES, web UI for batches in flight
services/plc-sim/              Python: OPC UA machining-cell PLC, command-driven
services/hvac-sim/             Python: BACnet/IP rooftop AHU simulator (bacpypes3)
emqx/base.hocon                rule engine + Timescale sinks (plc + hvac)
timescale/init.sql             plc_telemetry + hvac_telemetry hypertables + batches table
grafana/                       provisioned datasource + production-overview + facility-hvac dashboards
tools/provision_neuron.py      REST-API neuron provisioning (OPC UA on neuron-1/2, BACnet on neuron-3)
tools/place_order.py           dev helper: publish a JSON order from the CLI
```

## Quick start

Install Docker with Docker Compose and [uv](https://docs.astral.sh/uv/)
for the provisioning and command-line helpers. Run these commands from the
repository root:

```bash
make up           # docker compose up -d --build
# wait ~30s for the neurons to finish first-boot init
make provision    # configure all 3 neurons via REST
```

Open <http://localhost:8080> and wait for **All sources live**: both CNCs
and the HVAC unit must have fresh telemetry. Before starting a walkthrough,
let existing orders finish or abort them in MES so the CNCs are available.
`make help` lists the available targets.

App images are pinned to EMQX 6.3.1, NeuronEX 3.9.4, Grafana 13.2.2, and
TimescaleDB 2.30.1 on PostgreSQL 17.

Open:

- **Live showcase** — <http://localhost:8080/showcase> (guided order, fault/recovery, additional subscriber)
- **Demo home** — <http://localhost:8080> (live device-to-cloud map and guided walkthrough)
- **Demo tools** — <http://localhost:8080/tools> (architecture reference and product links)
- **Operator console** — <http://localhost:8080/console> (place orders, fault injection)
- **MES** — <http://localhost:8090> (batches in flight, HOLD / RESUME / ABORT)
- **Grafana** — <http://localhost:3000/d/production-overview> (machining) / <http://localhost:3000/d/facility-hvac> (HVAC)
- **EMQX dashboard** — <http://localhost:18083> (admin/admin)
- **Neuron UI** — neuron-1 OPC UA <http://localhost:8085>, neuron-2 OPC UA <http://localhost:8086>, neuron-3 BACnet/IP <http://localhost:8087> (admin/0000)

## Operating the demo

### Guided walkthrough

Click **Start walkthrough** for one narrated cycle, roughly one minute.
**Auto loop** repeats the cycle until **Reset walkthrough** is pressed.
One server-side controller owns the run across all browsers: opening another
tab does not create another order, and reloading or closing a tab does not
stop the loop.

1. Follow the map from the two OPC UA CNCs and the BACnet/IP HVAC unit,
   through their Neuron gateways, to EMQX and the applications.
2. Start the walkthrough. It submits one `program-a` order; MES assigns a
   CNC and commands `load → rough → finish → unload`.
3. The additional power dashboard connects as a separate MQTT subscriber.
   Its counters distinguish retained readings from new live updates. It
   reads the existing power topics without changing the devices or gateways.
4. During rough machining, the controller injects an over-temperature
   fault. Machining pauses, the fault reaches MES, and the controller clears
   it after about seven seconds so the order can continue.
5. Watch the order reach **completed**, then inspect **Follow one measurement**
   for a source tag, topic and payload, or **What just happened** for the
   observed events. Open MES or Grafana for the batch and telemetry views.

The controls also support a closer look during a walkthrough:

| Control | Action |
| --- | --- |
| **Add a dashboard** / **Disconnect** | Connect or disconnect the browser's independent MQTT power dashboard. This also works without a walkthrough. |
| **Inject fault** / **Clear fault** | Fault or recover the walkthrough's assigned CNC when its state permits. The guided cycle still performs its automatic recovery. |
| **Animate packets** / **Pause packets** | Enable or pause motion along active routes. The browser remembers the choice. |
| Full-screen button | Expand the page for a larger display. |
| **Reset walkthrough** | Stop the run or loop and return the presentation to its initial state; see reset details below. |

### Reading the data flow

Blue packets travel upward with telemetry and events. Purple packets travel
downward with orders and commands, following the route to the addressed CNC.
The HVAC unit supplies telemetry independently and is outside MES's
production allocation pool.

Packets highlight recently observed MQTT traffic; they do not represent
individual network packets or exact transit times. Idle routes stay quiet.
Motion starts paused when the browser requests reduced motion; use
**Animate packets** to enable it explicitly.

Source freshness and machine lifecycle are separate: an idle machine can
still send fresh telemetry, and a connected broker alone does not mean all
sources are healthy. Stale or unavailable readings are marked accordingly.
The additional dashboard gets its readings from its own MQTT connection,
and shows combined power only when both CNC readings are fresh.

### Manual operation

Reset any guided run first, then open the
[operator console](http://localhost:8080/console).

1. Select `program-a` for all four phases, or `program-quick` to skip finish
   machining. Set the quantity and place the order. `make place-order` is a
   command-line shortcut for one `program-a` order.
2. Follow the batch in [MES](http://localhost:8090) and the readings in
   [Grafana](http://localhost:3000/d/production-overview). MES dispatches
   commands through EMQX and Neuron to the assigned CNC.
3. To demonstrate recovery, use **Inject fault** on a running CNC in the
   operator console, then **Clear** to resume it. MES also provides
   **Hold**, **Resume** and **Abort** controls for the batch.
4. Wait for the order's status to become `completed`. A PLC's `completed`
   state can mark the end of an individual phase; the order status confirms
   that the entire recipe finished and its batch record was published.

Manual orders are not cancelled by **Reset walkthrough**. Finish them or
use **Abort** in MES before returning to the guided demonstration.

### Reset and recovery

**Reset walkthrough** stops the loop, aborts its own active order through
the normal MQTT → Neuron → OPC UA command path, and waits for a terminal
order status. It then clears the presentation and disconnects the added
dashboard. Database history, Neuron configuration, volumes and manually
created orders are preserved.

The same reset is available from the local command line:

```bash
curl --fail --silent --show-error -X POST http://localhost:8080/api/demo/reset \
  -H 'Content-Type: application/json' -d '{}'
```

Wait for reset to finish and the sources to be live before starting again.
If reset reports a recovery error, restore EMQX, Neuron and MES connectivity
and retry it; the previous order must be resolved before another run starts.

| Symptom | What to check |
| --- | --- |
| Sources stay stale or unavailable | Run `docker compose ps`, then `make provision` after all three Neuron services finish starting. Inspect the affected gateway's driver and tags in its Neuron UI. |
| Walkthrough cannot start | Check that all three sources are live, both CNCs are available, and no previous walkthrough order needs recovery. Finish or abort manual orders in MES. |
| Added dashboard waits for readings | Check EMQX's WebSocket listener at `ws://localhost:8083/mqtt` and fresh CNC telemetry. The dashboard has a separate connection from the page's status feed. |
| Active arrows have no moving packets | Click **Animate packets**; reduced-motion preferences pause animation by default. |

Useful diagnostics:

```bash
docker compose ps
docker compose logs --tail=100 operator mes plc-1 plc-2 hvac-1
docker compose logs --tail=100 emqx neuron-1 neuron-2 neuron-3
curl -i http://localhost:8080/readyz
```

`/readyz` returns HTTP 200 only when the broker is connected and all three
sources are fresh; otherwise it returns 503. `/healthz` checks the operator
HTTP service only.

## Configuration

Set `SHOWCASE_TITLE` and `SHOWCASE_EVENT` in the shell or a local `.env` file
to change the page title and subtitle, then run `make up` to apply them.

Compose accepts `ENTERPRISE`, `SITE` and `LINE` for the topic namespace.
Give `make provision` the same values in its shell environment; the
provisioning script does not read Compose's `.env` file. Keep the machining
and utilities areas consistent with the broker rules. The default namespace
needs no extra configuration.

The provisioner also accepts `NEURON_API_1` through `NEURON_API_3` for
alternative Neuron endpoints and `BACNET_HOST` for the HVAC device's numeric
IP address. Run one operator process: it owns the MQTT state and guided
controller. The supplied server uses one process with eight HTTP threads.

## Checking the demo

Run `make check` for the controller, reset, freshness and authentication
tests. With a running, provisioned stack and no active orders, run:

```bash
python3 tools/check_walkthrough.py
```

This live check creates test orders and checks completion, a second loop,
reset while faulted, and restart. It resets the walkthrough when finished.

Before a demonstration, also check the page at the intended display size:
complete a cycle, see retained and live updates in the added dashboard,
reload during a run to confirm it continues, and reset before starting
another order. Open the relevant product screens in advance.

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
```

Defaults: `ENTERPRISE=acme-mfg`, `SITE=plant-1`, `LINE=line-1`.
The CNC area is fixed to `machining` in Compose and provisioning to match
the broker rules. HVAC uses `utilities/compressor-room`.

## Upgrading an existing installation

Back up the database and `grafana-data` volume first. [Grafana 13 migrates
dashboard storage](https://grafana.com/docs/grafana/latest/upgrade-guide/upgrade-v13.0/),
so rolling back requires restoring the backup. PostgreSQL stays on major
version 17 to preserve data-volume compatibility.

```bash
docker compose pull
docker compose build --pull
docker compose up -d
docker compose exec timescaledb psql -X -U demo -d demo -v ON_ERROR_STOP=1 \
  -c 'ALTER EXTENSION timescaledb UPDATE;'
make provision
```

The extension update is required for an existing TimescaleDB database;
fresh volumes install the current extension automatically.

## Stopping or rebuilding from scratch

`make down` stops the services and preserves their named volumes. Use
`make up` to start them again.

For a fresh installation, the following commands **delete all demo volumes**,
including database history, Grafana state and Neuron configuration, then
recreate and provision the stack. Use **Reset walkthrough** for ordinary
repeated demonstrations.

```bash
make reset
make provision
```
