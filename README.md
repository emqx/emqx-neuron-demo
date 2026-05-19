# ERP ↔ MES ↔ PLC Demo

Batch process integration over MQTT (ERP ↔ MES ↔ PLC), built on EMQX and
EMQX Neuron.

The shop-floor scenario is a CNC machining cell — each PLC runs a multi-phase
part program (`load → rough → finish → unload`) and streams generic CNC
telemetry (spindle RPM, feed rate, coolant temperature, torque, power) — but
nothing in the stack is CNC-specific. Swap the metric names and phase names
and you have a heat-treat line, a mixing reactor, or any other batch-process
asset.

```
operator console (ERP panel) ──► EMQX ──► MES ──► EMQX ──► neuron-1 ──► plc-1 (OPC UA)
                                          │                                │
                                          │ batch records, order status    │
                                          ▼                                │
                                       Postgres (via Timescale)            │
                                                                           │
   plc-1 telemetry ◄──── OPC UA ───────────────────────────────────────────┘
                  │
                  ▼ MQTT (Neuron)
              EMQX rule engine ──► Timescale ──► Grafana
```

## Layout

```
docker-compose.yml             EMQX + 3 PLCs + 3 neurons + MES + operator + storage
services/operator/             Flask: ERP "place order" panel + PLC/order status
services/mes-sim/              Python: state-machine MES, web UI for batches in flight
services/plc-sim/              Python: OPC UA machining-cell PLC, command-driven
emqx/base.hocon                rule engine + Timescale sink
timescale/init.sql             plc_telemetry hypertable + batches table
grafana/                       provisioned datasource + production-overview dashboard
tools/provision_neuron.py      REST-API neuron provisioning (out-of-band fallback)
tools/place_order.py           dev helper: publish a JSON order from the CLI
```

## Quick start

```bash
make up           # docker compose up -d
# wait ~30s for the neurons to finish first-boot init
make provision    # configure all 3 neurons via REST
```

(`make help` lists every target.)

Open:
- **Demo home** — <http://localhost:8080> (architecture, demo flow, links into everything else)
- **Operator console** — <http://localhost:8080/console> (place orders, fault injection)
- **MES** — <http://localhost:8090> (batches in flight, HOLD / RESUME / ABORT)
- **Grafana** — <http://localhost:3000/d/production-overview>
- **EMQX dashboard** — <http://localhost:18083> (admin/admin)
- **Neuron UI** — <http://localhost:8085> (admin/0000; replicas on :8086, :8087)

Place an order from the operator console → MES picks it up → dispatches phase
commands to a PLC → PLC executes the recipe → batch record published.

## Demo narrative (for the live walkthrough)

1. **Architecture diagram** — talk through topics, namespaces, sinks.
2. **Place an order** — operator console, recipe `program-a`, qty 1.
   Show the order JSON on `acme-mfg/plant-1/orders/<id>` via `mosquitto_sub`.
3. **MES dispatches** — MES UI shows the batch transitioning idle → running.
4. **PLC executes** — Grafana shows spindle RPM, feed rate, and coolant
   temperature evolve through the load → rough → finish → unload phases.
5. **Batch completion** — batch record published; order status flips to
   `completed`; operator console picks up the retained status.

## Topics

```
acme-mfg/<site>/orders/<id>                                              # ERP → MES
acme-mfg/<site>/orders/<id>/status                                       # MES → ERP (retained)
acme-mfg/<site>/<area>/<line>/<plc>/state                                # PLC (retained, synthesized)
acme-mfg/<site>/<area>/<line>/<plc>/telemetry                            # PLC via Neuron
acme-mfg/<site>/<area>/<line>/<plc>/state-events                         # PLC transitions via Neuron
acme-mfg/<site>/<area>/<line>/<plc>/batch/<bid>/phase/<p>/command        # MES → PLC
acme-mfg/<site>/batches/<bid>/record                                     # MES (retained)
```

Defaults: `ENTERPRISE=acme-mfg`, `SITE=plant-1`, `AREA=machining`,
`LINE=line-1`. Override via env on the `mes`, `operator`, and `plc-*`
services (and `tools/provision_neuron.py`) to re-skin the topic taxonomy.

## Reset

```bash
make reset
make provision
```
