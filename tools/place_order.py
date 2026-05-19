# /// script
# requires-python = ">=3.11"
# dependencies = ["paho-mqtt==2.1.0"]
# ///
"""Dev helper: publish a JSON order to the orders topic.

Not user-facing. Step 2 (operator console) replaces this with a UI panel.

    uv run tools/place_order.py --recipe program-a --qty 1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone

import paho.mqtt.publish as publish


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--enterprise", default="acme-mfg")
    p.add_argument("--site", default="plant-1")
    p.add_argument("--recipe", default="program-a")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--order-id")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    args = p.parse_args()

    order_id = args.order_id or f"ORD-{int(time.time())}-{uuid.uuid4().hex[:4]}"
    payload = {
        "order_id": order_id,
        "recipe_id": args.recipe,
        "qty": args.qty,
        "placed_at": utc_now(),
    }
    topic = f"{args.enterprise}/{args.site}/orders/{order_id}"
    publish.single(topic, json.dumps(payload), qos=1, retain=True,
                   hostname=args.host, port=args.port)
    print(f"placed: {topic}")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
