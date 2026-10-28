#!/usr/bin/env python3
"""Exercise a provisioned demo: completion, repeated loop, fault reset, restart.

Creates test orders. Use a rehearsal instance, not a staffed demonstration.
For a hosted instance, pass its base URL and BOOTH_TEST_PASSWORD in the env.
"""
import argparse
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from init_security import load_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8080")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, data=None):
        headers = {"Content-Type": "application/json", "Origin": base,
                   "X-Demo-Admin": os.getenv("DEMO_ADMIN_KEY", load_env().get("DEMO_ADMIN_KEY", ""))}
        req = urllib.request.Request(base + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
        with opener.open(req, timeout=8) as response:
            return json.load(response)

    password = os.getenv("BOOTH_TEST_PASSWORD")
    if password:
        body = urllib.parse.urlencode({"password": password}).encode()
        with opener.open(urllib.request.Request(base + "/login", data=body,
                                               headers={"Origin": base}), timeout=8) as response:
            response.read()

    def wait_for(predicate, timeout):
        deadline = time.monotonic() + timeout
        previous = None
        while time.monotonic() < deadline:
            state = request("/api/demo/state")
            scene = state["scene"]
            key = (scene["iteration"], scene["step"])
            if key != previous:
                print(f"Run {key[0]}: {key[1]}", flush=True)
                previous = key
            if scene["step"] == "error":
                raise RuntimeError(scene["caption"])
            if predicate(state):
                return state
            time.sleep(0.4)
        raise TimeoutError("The expected walkthrough transition did not arrive.")

    state = request("/api/demo/state")
    if state["scene"]["active"]:
        raise RuntimeError("A walkthrough is already active; leave it undisturbed and retry later.")
    wait_for(lambda s: s["ready"], 30)
    try:
        request("/api/demo/start", {"loop": True})
        wait_for(lambda s: s["scene"]["iteration"] == 1 and s["scene"]["step"] == "complete" and s["order"].get("state") == "completed", 110)
        state = wait_for(lambda s: s["scene"]["iteration"] >= 2 and s["scene"]["step"] == "fault", 60)
        reset_version = state["reset_version"]
        request("/api/demo/reset", {})
        wait_for(lambda s: s["scene"]["step"] == "ready" and s["reset_version"] > reset_version, 20)
        request("/api/demo/start", {"loop": False})
        wait_for(lambda s: s["scene"]["step"] == "share", 40)
        print("PASS: completion, repeat loop, reset during fault and restart", flush=True)
    finally:
        request("/api/demo/reset", {})
        wait_for(lambda s: s["scene"]["step"] == "ready" and not s["scene"]["active"], 20)


if __name__ == "__main__":
    main()
