# Booth demonstration

The showcase presents the existing two OPC UA CNC simulators, BACnet HVAC
simulator, three Neuron instances and EMQX on one screen. Equipment is
simulated; acquisition, MQTT distribution, MES commands and state changes
use the real demo stack. Huawei hardware and EMQX Edge are outside this scope.

## Start locally

```bash
make up
make provision
```

Open <http://localhost:8080/showcase>. Wait for **All sources live**. The
existing operator console, MES, Neuron, EMQX, Grafana and namespace views
remain available under **Product screens**. Direct service ports now bind
to `127.0.0.1` so they are not accidentally published on a server's public IP.

**Start walkthrough** runs one narrated cycle, roughly one minute plus time
for discussion. **Auto loop** repeats it until **Reset walkthrough** is
pressed. One server-side controller owns the run: opening another browser
does not create a second order. Reloading the browser does not stop a loop.

1. Point out the two industrial protocols and the shared namespace.
2. Start the walkthrough. An order reaches MES; the CNC begins its phases.
3. The additional dashboard opens a separate MQTT connection and subscribes
   to the existing per-machine power topics. It receives retained readings
   and live updates. **Add a dashboard** also works independently of a run.
4. The controller injects an over-temperature fault during rough machining.
   Show the machine's fault state and the MES view, then automatic recovery.
5. The order completes. Inspect one source tag, its MQTT topic and payload,
   or open a product management screen for a deeper conversation.

The power dashboard's values come from its own MQTT client, not the
showcase's HTTP API. Its counter distinguishes retained values from new
live updates. Telemetry and connection freshness are shown separately from
machine lifecycle state. A connected broker alone does not mean a source
is healthy.

## Reset and recovery

**Reset walkthrough** cancels the loop and sends an abort through the usual
MES-command → EMQX-rule → Neuron → OPC-UA path for the walkthrough's order.
It waits for a terminal order status, clears the presentation, and disconnects
the added browser dashboard. The PLC keeps terminal lifecycle events visible until the next command, so
Neuron and MES can observe the abort. It does not erase database history, Neuron
configuration or volumes, and does not abort manually created orders.

`make booth-reset` performs the same action on the unauthenticated local
profile. For the hosted profile use the signed-in browser button.

If reset cannot confirm the abort, the screen reports a recovery error.
Restore broker/Neuron/MES connectivity and retry reset. Do not start a new
run while the previous order is unresolved. Use the existing product screens
to inspect the current phase. `make reset` remains a destructive development
reset, not a between-demo control.

Keep a downloaded MP4 walkthrough on the presenter laptop for venue-network
failures. The live page does not silently replace unavailable data with
recorded or invented values. An optional local recording can be served from
`services/operator/static/recording.mp4`; that filename is gitignored.

## Host behind HTTPS

The optional Compose overlay adds Caddy with automatic TLS and a shared
password session protecting the showcase, MQTT WebSocket and product UIs.
Product UIs use separate subdomains, preserving their native asset/API paths.
Session cookies leave product `Authorization` headers intact.

1. Choose a **dedicated** domain, for example `factory.example.com`. Point it
   and `mes`, `grafana`, `emqx`, `neuron-1`, `neuron-2`, `neuron-3` subdomains
   to the host. The session cookie is shared only under this demo domain.
2. Generate credentials locally on the host:

   ```bash
   uv run tools/booth_credentials.py factory.example.com
   ```

   The tool prompts for a password and writes a hash and random session
   secret to a new mode-0600 `.env.booth`. It refuses to overwrite a file.
   The file is gitignored; do not commit it.
3. Start and provision:

   ```bash
   make booth-up
   make provision
   ```

   For a custom namespace, give provisioning the same `ENTERPRISE`, `SITE`
   and `LINE` values as Compose. The machine area is fixed to `machining` in
   Compose and provisioning to match the broker rules. Default values need
   no extra flags.
4. Open `https://factory.example.com`. Sign in with the demo password. The
   homepage opens the showcase; product screens share the session and keep
   their normal product login where applicable. Change product passwords
   through those UIs before sharing staff access.

Only Caddy exposes public ports 80/443. Provisioning and raw product ports
remain on loopback; do not add public mappings for MQTT, Postgres or admin
ports. All browser dependencies are served locally, including pinned MQTT.js.
The browser uses same-origin `wss://<demo-domain>/mqtt` when hosted.

The password session lasts 16 hours. Reload and sign in again if it expires.
Regenerating `DEMO_SESSION_SECRET` and recreating the operator invalidates
all existing sessions. This is a shared staff demo, not a public multi-user
production application. No hosting account, domain or remote deployment is
created by this repository.

## Configuration

| Setting | Purpose |
| --- | --- |
| `SHOWCASE_TITLE`, `SHOWCASE_EVENT` | Screen title and event label |
| `SHOWCASE_HOME=1` | Open the showcase from `/`; tools stay at `/tools` |
| `MQTT_PROXY=1` | Use same-origin `/mqtt` even for an HTTP rehearsal |
| `MQTT_PUBLIC_URL` | Explicit WebSocket URL for another reverse proxy |
| `MES_PUBLIC_URL`, `GRAFANA_PUBLIC_URL`, `EMQX_PUBLIC_URL` | Browser-facing product URLs |
| `NEURON_1_PUBLIC_URL` through `NEURON_3_PUBLIC_URL` | Browser-facing Neuron URLs |
| `NEURON_API_1` through `NEURON_API_3`, `BACNET_HOST` | Optional provisioning endpoints and numeric BACnet device IP |
| `DEMO_PASSWORD_HASH`, `DEMO_SESSION_SECRET`, `PUBLIC_BASE_URL` | Shared session authentication; set together |
| `DEMO_COOKIE_DOMAIN`, `DEMO_COOKIE_SECURE` | Shared subdomain cookie; hosted profile requires HTTPS |

Run one operator process: it owns MQTT state and the guided controller.
The supplied Waitress server uses one process with eight HTTP threads.
The hosted profile trusts one proxy hop on its private app listener, including
the forwarded HTTPS scheme needed for origin checks and secure sessions.

## Rehearsal

- Run `make check` for controller, freshness and authentication tests.
- Run `python3 tools/check_walkthrough.py` against a local rehearsal stack
  for completion, a second loop, reset while faulted and restart. This creates
  test orders. For hosting use `--url https://<demo-domain>` and set
  `BOOTH_TEST_PASSWORD` in the environment.
- Start from a provisioned stack; check all three sources are fresh.
- Complete a walkthrough and see retained plus live values in the added dashboard.
- Run two loop iterations; reset while faulted, then start another order.
- Reload the screen during a run; open a second tab and confirm only one run.
- Stop a source briefly: its readings must become stale, not remain green.
- From a clean external browser, verify login, WSS, every product UI and its API.
- Try the actual display at its intended resolution and test the local MP4 fallback.

References: [Caddy forward authentication](https://caddyserver.com/docs/caddyfile/directives/forward_auth),
[Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).
