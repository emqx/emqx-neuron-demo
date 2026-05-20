# ERP ↔ MES ↔ PLC Demo

Batch process integration over MQTT (ERP ↔ MES ↔ PLC), built on EMQX and
EMQX Neuron.

The shop-floor scenario is a CNC machining cell — two PLCs running a
multi-phase part program (`load → rough → finish → unload`) and streaming
generic CNC telemetry (spindle RPM, feed rate, coolant temperature, torque,
power). Alongside them, a third neuron talks **BACnet/IP** to a simulated
facility HVAC unit, showing the same Neuron + EMQX stack ingesting both
shop-floor OPC UA and brownfield building-automation BACnet side-by-side.

```
operator console (ERP panel) ──► EMQX ──► MES ──► EMQX ──► neuron-1 ──► plc-1 (OPC UA)
                                          │                neuron-2 ──► plc-2 (OPC UA)
                                          │ batch records, order status
                                          ▼
                                       Postgres (via Timescale)
                                          ▲
                                          │ MQTT (Neuron)
              EMQX rule engine ◄──────────┴──── neuron-3 ──► hvac-1 (BACnet/IP)
```

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
- **Grafana** — <http://localhost:3000/d/production-overview> (machining) / <http://localhost:3000/d/facility-hvac> (HVAC)
- **EMQX dashboard** — <http://localhost:18083> (admin/admin)
- **Neuron UI** — neuron-1 OPC UA <http://localhost:8085>, neuron-2 OPC UA <http://localhost:8086>, neuron-3 BACnet/IP <http://localhost:8087> (admin/0000)

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
acme-mfg/<site>/machining/<line>/<plc>/state                             # PLC (retained, synthesized)
acme-mfg/<site>/machining/<line>/<plc>/telemetry                         # PLC via Neuron (OPC UA)
acme-mfg/<site>/machining/<line>/<plc>/state-events                      # PLC transitions via Neuron
acme-mfg/<site>/machining/<line>/<plc>/batch/<bid>/phase/<p>/command     # MES → PLC
acme-mfg/<site>/batches/<bid>/record                                     # MES (retained)
acme-mfg/<site>/utilities/compressor-room/hvac-1/telemetry               # HVAC via Neuron (BACnet/IP)
```

Defaults: `ENTERPRISE=acme-mfg`, `SITE=plant-1`, `AREA=machining`,
`LINE=line-1`. Override via env on the `mes`, `operator`, and `plc-*`
services (and `tools/provision_neuron.py`) to re-skin the topic taxonomy.
The HVAC side uses `HVAC_AREA=utilities`, `HVAC_LINE=compressor-room`.

## Reset

```bash
make reset
make provision
```
