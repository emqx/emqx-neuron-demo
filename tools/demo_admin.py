#!/usr/bin/env python3
"""Local operator controls; the administrator key never goes into a browser."""
import argparse
import json
import os
import urllib.request
from init_security import load_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("reset", "start", "loop"))
    parser.add_argument("--url", default="http://localhost:8080")
    args = parser.parse_args()
    key = os.getenv("DEMO_ADMIN_KEY", load_env().get("DEMO_ADMIN_KEY", ""))
    if not key:
        raise SystemExit("Run make security-init first.")
    action = "start" if args.action == "loop" else args.action
    payload = json.dumps({"loop": args.action == "loop"}).encode()
    req = urllib.request.Request(args.url.rstrip("/") + "/api/demo/" + action, data=payload,
                                 headers={"Content-Type": "application/json", "X-Demo-Admin": key})
    with urllib.request.urlopen(req, timeout=10) as response:
        print(response.read().decode())


if __name__ == "__main__":
    main()
