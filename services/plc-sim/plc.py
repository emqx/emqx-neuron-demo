# /// script
# requires-python = ">=3.11"
# dependencies = ["opcua==0.98.13"]
# ///
"""OPC UA CNC machining-cell PLC simulator (pure OPC UA — no MQTT).

Lifecycle is exposed two ways:
  - Individual tags (phase_state, current_batch_id, current_phase, state_reason)
    polled at 1 Hz on the telemetry driver — for the operator console's
    per-PLC status view.
  - One composite tag `phase_event_json` carrying a JSON snapshot of the
    transition. PLC writes this on every state change. Neuron subscribes
    (OPC UA monitored item) and pushes one MQTT event per change to the
    state-events topic — what MES consumes.


Single-axis machining cell PLC that idles until commands arrive (written
by Neuron into the PLC's OPC UA tags).

Control plane is OPC UA writable tags: writing a new value to `command_seq`
triggers the PLC to read `command_action` / `command_phase` /
`command_batch_id` and apply it. Same pattern for fault inject/clear via
`fault_*` tags. Neuron handles MQTT↔OPC UA both directions.

State machine:
    idle → running → holding → running         (HOLD / RESUME from MES)
                  → faulted → running          (fault inject / clear from operator)
                  → aborted → idle             (ABORT from MES)
                  → completed → idle           (phase finishes naturally)

Phase elapsed time only counts while in `running`; HOLD and FAULT pauses
preserve progress.

OPC UA tag layout (order MUST match tools/provision_neuron.py:TAGS):
    namespace 2 / object 2!1 MachineMetrics
    var  2!2  spindle_rpm        DOUBLE  read
    var  2!3  feed_mm_min        DOUBLE  read
    var  2!4  coolant_temp_c     DOUBLE  read
    var  2!5  torque_nm          DOUBLE  read
    var  2!6  power_kw           DOUBLE  read
    var  2!7  state              STRING  read   (operating mode label, for Grafana)
    var  2!8  plc_id             STRING  read
    var  2!9  axis_id            STRING  read
    var  2!10 phase_state        STRING  read   (lifecycle: idle|running|holding|faulted|completed|aborted)
    var  2!11 current_batch_id   STRING  read
    var  2!12 current_phase      STRING  read
    var  2!13 state_reason       STRING  read   (e.g. fault reason)
    var  2!14 command_action     STRING  write  (start|hold|resume|abort)
    var  2!15 command_phase      STRING  write
    var  2!16 command_batch_id   STRING  write
    var  2!17 command_seq        STRING  write  (incremented to trigger; PLC polls for change)
    var  2!18 fault_action       STRING  write  (inject|clear)
    var  2!19 fault_type         STRING  write
    var  2!20 fault_seq          STRING  write
    var  2!21 phase_event_json   STRING  read   (subscribe — JSON snapshot per transition)
"""
from __future__ import annotations

import json
import os
import queue
import time
from dataclasses import dataclass
from typing import Optional

from opcua import Server


OPCUA_PORT  = int(os.getenv("OPCUA_PORT", "4840"))
PLC_ID      = os.getenv("PLC_ID", "plc-1")
AXIS_ID     = os.getenv("AXIS_ID", "axis-01")
TICK_SECS   = float(os.getenv("TICK_SECONDS", "1"))

# Telemetry envelope — CNC machining-cell shape.
T_AMBIENT          = 22.0
FAULT_COOLANT_C    = 60.0


@dataclass
class Phase:
    name: str
    duration: float


PHASES = {
    "load":   Phase("load",    5.0),
    "rough":  Phase("rough",  15.0),
    "finish": Phase("finish", 12.0),
    "unload": Phase("unload",  5.0),
}


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def sample(phase_name: str, t_in_phase: float, duration: float,
           coolant_carry: float) -> dict:
    progress = t_in_phase / duration if duration > 0 else 1.0
    if phase_name == "load":
        return {"spindle_rpm":    0.0,
                "feed_mm_min":    0.0,
                "coolant_temp_c": round(lerp(T_AMBIENT, T_AMBIENT + 2, progress), 2),
                "torque_nm":      0.0,
                "power_kw":       0.0}
    if phase_name == "rough":
        return {"spindle_rpm":    round(lerp(0, 3000, min(progress * 4, 1.0)), 1),
                "feed_mm_min":    500.0,
                "coolant_temp_c": round(lerp(T_AMBIENT + 2, 50.0, progress), 2),
                "torque_nm":      round(lerp(20.0, 28.0, progress), 2),
                "power_kw":       round(lerp(4.0, 7.0, progress), 2)}
    if phase_name == "finish":
        return {"spindle_rpm":    6000.0,
                "feed_mm_min":    150.0,
                "coolant_temp_c": round(lerp(50.0, 42.0, progress), 2),
                "torque_nm":      round(lerp(15.0, 6.0, progress), 2),
                "power_kw":       round(lerp(3.0, 2.0, progress), 2)}
    if phase_name == "unload":
        return {"spindle_rpm":    0.0,
                "feed_mm_min":    0.0,
                "coolant_temp_c": round(lerp(coolant_carry, T_AMBIENT + 5, progress), 2),
                "torque_nm":      0.0,
                "power_kw":       0.0}
    # idle
    return {"spindle_rpm":    0.0,
            "feed_mm_min":    0.0,
            "coolant_temp_c": coolant_carry if coolant_carry else T_AMBIENT,
            "torque_nm":      0.0,
            "power_kw":       0.0}


class PlcState:
    IDLE = "idle"
    RUNNING = "running"
    HOLDING = "holding"
    FAULTED = "faulted"


def main() -> None:
    server = Server()
    endpoint = f"opc.tcp://0.0.0.0:{OPCUA_PORT}/freeopcua/server/"
    server.set_endpoint(endpoint)
    server.set_server_name(f"CNC machining-cell PLC {PLC_ID}")

    idx = server.register_namespace("http://demo.local/machining")
    obj = server.nodes.objects.add_object(idx, "MachineMetrics")

    # Explicit NodeIds (ns!nid) — must match tools/provision_neuron.py:TAGS.
    # python-opcua's auto-assignment isn't reliably sequential across many
    # add_variable() calls, so we pin them.
    def var(nid: int, name: str, value):
        return obj.add_variable(f"ns={idx};i={nid}", name, value)

    nodes = {
        "spindle_rpm":       var(2,  "spindle_rpm",      0.0),
        "feed_mm_min":       var(3,  "feed_mm_min",      0.0),
        "coolant_temp_c":    var(4,  "coolant_temp_c",   T_AMBIENT),
        "torque_nm":         var(5,  "torque_nm",        0.0),
        "power_kw":          var(6,  "power_kw",         0.0),
        "state":             var(7,  "state",            "idle"),
        "plc_id":            var(8,  "plc_id",           PLC_ID),
        "axis_id":           var(9,  "axis_id",          AXIS_ID),
        "phase_state":       var(10, "phase_state",      "idle"),
        "current_batch_id":  var(11, "current_batch_id", ""),
        "current_phase":     var(12, "current_phase",    ""),
        "state_reason":      var(13, "state_reason",     ""),
        "command_action":    var(14, "command_action",   ""),
        "command_phase":     var(15, "command_phase",    ""),
        "command_batch_id":  var(16, "command_batch_id", ""),
        "command_seq":       var(17, "command_seq",      ""),
        "fault_action":      var(18, "fault_action",     ""),
        "fault_type":        var(19, "fault_type",       ""),
        "fault_seq":         var(20, "fault_seq",        ""),
        "phase_event_json":  var(21, "phase_event_json", ""),
    }
    # Mark writable: clients (Neuron) can write; PLC still reads via own API.
    for tag in ("command_action", "command_phase", "command_batch_id", "command_seq",
                "fault_action", "fault_type", "fault_seq"):
        nodes[tag].set_writable()

    server.start()
    print(f"OPC UA endpoint: {endpoint}", flush=True)
    print(f"PLC: {PLC_ID}  Axis: {AXIS_ID}", flush=True)

    # PLC state.
    unit_state = PlcState.IDLE
    current_batch: Optional[str] = None
    current_phase: Optional[str] = None
    fault_type: Optional[str] = None
    coolant_carry = T_AMBIENT
    last_values = sample("idle", 0, 1, coolant_carry)
    elapsed_in_phase = 0.0
    last_running_at: Optional[float] = None
    last_command_seq = ""
    last_fault_seq = ""
    # Track what we last wrote to state tags so we only write on actual change.
    last_published_phase_state = ""
    last_published_batch_id = ""
    last_published_phase = ""
    last_published_reason = ""

    cmd_queue: "queue.Queue[tuple]" = queue.Queue()

    def write_state_tags(phase_state: str, batch_id: str, phase: str, reason: str = ""):
        nonlocal last_published_phase_state, last_published_batch_id
        nonlocal last_published_phase, last_published_reason
        changed = (phase_state != last_published_phase_state or
                   batch_id    != last_published_batch_id or
                   phase       != last_published_phase or
                   reason      != last_published_reason)
        if phase_state != last_published_phase_state:
            nodes["phase_state"].set_value(phase_state)
            last_published_phase_state = phase_state
        if batch_id != last_published_batch_id:
            nodes["current_batch_id"].set_value(batch_id)
            last_published_batch_id = batch_id
        if phase != last_published_phase:
            nodes["current_phase"].set_value(phase)
            last_published_phase = phase
        if reason != last_published_reason:
            nodes["state_reason"].set_value(reason)
            last_published_reason = reason
        if changed:
            # Single composite event tag; Neuron's events driver subscribes to
            # this and pushes one MQTT message per transition. Emitted for
            # every state change, including idle ones — downstream consumers
            # (mes-sim) already filter out events without batch_id.
            snapshot = json.dumps({
                "plc_id":   PLC_ID,
                "state":    phase_state,
                "batch_id": batch_id,
                "phase":    phase,
                "reason":   reason,
            })
            nodes["phase_event_json"].set_value(snapshot)

    def enter_running():
        nonlocal unit_state, last_running_at
        unit_state = PlcState.RUNNING
        last_running_at = time.monotonic()

    def accumulate_elapsed():
        nonlocal elapsed_in_phase, last_running_at
        if last_running_at is not None:
            elapsed_in_phase += time.monotonic() - last_running_at
            last_running_at = None

    def reset_phase_state():
        nonlocal current_batch, current_phase, fault_type, elapsed_in_phase
        nonlocal last_running_at
        current_batch = None
        current_phase = None
        fault_type = None
        elapsed_in_phase = 0.0
        last_running_at = None

    write_state_tags("idle", "", "")

    try:
        while True:
            # Poll command tags; act on changed seq.
            cmd_seq = nodes["command_seq"].get_value() or ""
            if cmd_seq and cmd_seq != last_command_seq:
                action = nodes["command_action"].get_value() or ""
                phase = nodes["command_phase"].get_value() or ""
                batch_id = nodes["command_batch_id"].get_value() or ""
                if action and phase and batch_id:
                    cmd_queue.put(("phase", batch_id, phase, action))
                    print(f"cmd seq={cmd_seq} {action} batch={batch_id} phase={phase}",
                          flush=True)
                last_command_seq = cmd_seq

            fault_seq = nodes["fault_seq"].get_value() or ""
            if fault_seq and fault_seq != last_fault_seq:
                action = nodes["fault_action"].get_value() or ""
                ftype = nodes["fault_type"].get_value() or "over_temp"
                if action:
                    cmd_queue.put(("fault", action, ftype))
                    print(f"fault seq={fault_seq} {action} type={ftype}", flush=True)
                last_fault_seq = fault_seq

            # Drain queue.
            try:
                while True:
                    item = cmd_queue.get_nowait()
                    if item[0] == "phase":
                        _, batch_id, phase, action = item
                        if action == "start" and unit_state == PlcState.IDLE \
                                and phase in PHASES:
                            current_batch = batch_id
                            current_phase = phase
                            elapsed_in_phase = 0.0
                            enter_running()
                            write_state_tags("running", batch_id, phase, "")
                        elif action == "hold" and unit_state == PlcState.RUNNING \
                                and current_batch == batch_id:
                            accumulate_elapsed()
                            unit_state = PlcState.HOLDING
                            write_state_tags("holding", batch_id, phase, "")
                        elif action == "resume" and unit_state == PlcState.HOLDING \
                                and current_batch == batch_id:
                            enter_running()
                            write_state_tags("running", batch_id, phase, "")
                        elif action == "abort" and current_batch == batch_id:
                            accumulate_elapsed()
                            write_state_tags("aborted", batch_id,
                                             current_phase or phase, "")
                            reset_phase_state()
                            unit_state = PlcState.IDLE
                            # leave terminal "aborted" briefly visible for one
                            # tick so MES sees the transition, then idle.
                    elif item[0] == "fault":
                        _, action, ftype = item
                        if action == "inject" and unit_state == PlcState.RUNNING:
                            accumulate_elapsed()
                            unit_state = PlcState.FAULTED
                            fault_type = ftype
                            write_state_tags("faulted", current_batch or "",
                                             current_phase or "", ftype)
                        elif action == "clear" and unit_state == PlcState.FAULTED:
                            fault_type = None
                            enter_running()
                            write_state_tags("running", current_batch or "",
                                             current_phase or "", "")
            except queue.Empty:
                pass

            # Tick: update telemetry + handle phase completion.
            if unit_state == PlcState.RUNNING and current_batch and current_phase:
                phase = PHASES[current_phase]
                t_in_phase = elapsed_in_phase + (
                    time.monotonic() - last_running_at if last_running_at else 0)
                values = sample(phase.name, t_in_phase, phase.duration, coolant_carry)
                coolant_carry = values["coolant_temp_c"]
                last_values = values
                state_str = phase.name
                if t_in_phase >= phase.duration:
                    write_state_tags("completed", current_batch, current_phase, "")
                    print(f"complete batch={current_batch} phase={current_phase}",
                          flush=True)
                    reset_phase_state()
                    unit_state = PlcState.IDLE
                    # idle state will be written next tick (after the
                    # "completed" event is visible to MES).
                    state_str = "idle"
            elif unit_state == PlcState.HOLDING:
                values = last_values
                state_str = "holding"
            elif unit_state == PlcState.FAULTED:
                values = dict(last_values)
                if fault_type == "over_temp":
                    values["coolant_temp_c"] = FAULT_COOLANT_C
                state_str = "faulted"
            else:  # IDLE
                values = sample("idle", 0, 1, coolant_carry)
                last_values = values
                state_str = "idle"
                # Move out of any terminal-state-display back to idle.
                if last_published_phase_state in ("completed", "aborted"):
                    write_state_tags("idle", "", "", "")

            for tag, value in values.items():
                nodes[tag].set_value(value)
            nodes["state"].set_value(state_str)

            print(f"{PLC_ID}/{AXIS_ID} {state_str:10s} "
                  f"RPM={values['spindle_rpm']:6.1f} "
                  f"F={values['feed_mm_min']:5.1f} "
                  f"T={values['coolant_temp_c']:5.2f} "
                  f"τ={values['torque_nm']:5.2f} "
                  f"P={values['power_kw']:4.2f}",
                  flush=True)
            time.sleep(TICK_SECS)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
