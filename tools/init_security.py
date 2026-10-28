#!/usr/bin/env python3
"""Create local demo secrets and broker ACLs without replacing existing secrets."""
import csv
import os
from pathlib import Path
import re
import secrets
import subprocess
import json

ROOT = Path(__file__).resolve().parents[1]


def load_env():
    values = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


def main():
    values = load_env()
    keys = ["DEMO_SESSION_SECRET", "DEMO_ADMIN_KEY",
            "EMQX_ADMIN_PASSWORD", "NEURON_ADMIN_PASSWORD", "GRAFANA_ADMIN_PASSWORD",
            "GRAFANA_DB_PASSWORD", "MQTT_OPERATOR_PASSWORD", "MQTT_MES_PASSWORD",
            "MQTT_NEURON_1_PASSWORD", "MQTT_NEURON_2_PASSWORD", "MQTT_NEURON_3_PASSWORD"]
    missing = {key: secrets.token_hex(24) for key in keys if not values.get(key)}
    if missing:
        fd = os.open(ROOT / ".env", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as file:
            file.write("\n# Generated local demo credentials; do not commit.\n")
            for key, value in missing.items():
                file.write(f"{key}={value}\n")
        values.update(missing)
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True, mode=0o700)
    private_key = runtime / "mqtt-signing-key.pem"
    if not private_key.exists():
        subprocess.run(["openssl", "genrsa", "-out", str(private_key), "2048"], check=True, capture_output=True)
        private_key.chmod(0o600)
    public_key = subprocess.run(["openssl", "rsa", "-in", str(private_key), "-pubout"],
                                check=True, capture_output=True, text=True).stdout
    # Only the public key reaches EMQX. Its Viewer configuration export can
    # include authentication settings, so a shared HMAC secret is unsuitable.
    auth = {"mechanism": "jwt", "use_jwks": False, "algorithm": "public-key",
            "public_key": public_key, "from": "password", "disconnect_after_expire": True,
            "verify_claims": {"username": "${username}", "clientid": "${clientid}"}}
    (runtime / "ws-auth.hocon").write_text(json.dumps({"listeners": {"ws": {"default": {"authentication": [auth]}}}}))
    (runtime / "ws-auth.hocon").chmod(0o644)
    # Bind mounts need to be readable by the non-root broker container. The
    # containing host directory is private; never serve it over HTTP.
    with (runtime / "mqtt-users.csv").open("w") as file:
        writer = csv.writer(file)
        writer.writerow(["user_id", "password", "is_superuser"])
        for user in ["operator", "mes", "neuron-1", "neuron-2", "neuron-3"]:
            writer.writerow([user, values["MQTT_" + user.upper().replace("-", "_") + "_PASSWORD"], "false"])
    enterprise = os.getenv("ENTERPRISE", values.get("ENTERPRISE", "acme-mfg"))
    site = os.getenv("SITE", values.get("SITE", "plant-1"))
    line = os.getenv("LINE", values.get("LINE", "line-1"))
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in (enterprise, site, line)):
        raise SystemExit("ENTERPRISE, SITE and LINE must be plain MQTT topic segments.")
    prefix = f"{enterprise}/{site}"
    machine = f"{prefix}/machining/{line}"
    rules = []
    def allow(user, action, topics):
        quoted = ", ".join('"' + topic + '"' for topic in topics)
        rules.append(f'{{allow, {{username, "{user}"}}, {action}, [{quoted}]}}.')
    allow("operator", "subscribe", [prefix + "/#"])
    device_bases = [machine + "/plc-1", machine + "/plc-2", prefix + "/utilities/compressor-room/hvac-1"]
    allow("operator", "publish", [prefix + "/orders/+", machine + "/+/_demo/fault", machine + "/+/batch/+/phase/+/command",
                                    *(base + suffix for base in device_bases for suffix in ("/cmd/req", "/control/#"))])
    allow("mes", "subscribe", [prefix + "/orders/+", machine + "/+/state-events", machine + "/+/state", machine + "/+/telemetry", machine + "/+/control/reservation"])
    allow("mes", "publish", [prefix + "/orders/+", prefix + "/orders/+/status", prefix + "/batches/+/record", machine + "/+/batch/+/phase/+/command", machine + "/+/control/reservation/ack"])
    for i in (1, 2, 3):
        base = f"{machine}/plc-{i}" if i < 3 else f"{prefix}/utilities/compressor-room/hvac-1"
        # The 4.0.0 MQTT binary still uses this default driver read topic
        # even when driver-topic-prefix is configured. Only gateway identities
        # may subscribe; neither browsers nor operator/MES may publish here.
        compatibility = ["neuron/uns-mqtt/read/req"]
        allow(f"neuron-{i}", "subscribe", [base + "/cmd/req", base + "/_neuron/#", *compatibility])
        allow(f"neuron-{i}", "publish", [base + suffix for suffix in ("/telemetry", "/state-events", "/cmd/ack", "/_neuron/#")])
    rules.append("{deny, all, all, [\"#\"]}.")
    (runtime / "acl.conf").write_text("\n".join(rules) + "\n")
    for name in ("mqtt-users.csv", "acl.conf"):
        (runtime / name).chmod(0o644)
    print("Local credentials ready in .env; broker credentials and ACLs ready in .runtime/.")


if __name__ == "__main__":
    main()
