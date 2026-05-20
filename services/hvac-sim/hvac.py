# /// script
# requires-python = ">=3.11"
# dependencies = ["bacpypes3==0.0.106", "ifaddr"]
# ///
"""BACnet/IP HVAC simulator (rooftop AHU + chilled-water side).

Pure BACnet — no MQTT. EMQX Neuron's BACnet/IP driver polls Present_Value
on these objects and publishes JSON to MQTT.

Object map (address must match tools/provision_neuron.py):

    AI 0   supply_air_temp_c      °C, leaves the cooling coil
    AI 1   return_air_temp_c      °C, conditioned space return
    AI 2   outside_air_temp_c     °C, ambient (slow sinusoid)
    AI 3   chilled_water_temp_c   °C, supply chilled water
    AI 4   fan_kw                 kW, supply fan power
    AI 5   filter_dp_pa           Pa, filter pressure drop (drifts up)

    BV 0   unit_running           active=on, inactive=off
    BV 1   fault_active           active when supply far from setpoint

    MSV 0  mode_actual            1=off 2=cool 3=heat 4=fan

    AV 0   temp_setpoint_c        writable, °C
    AV 1   fan_speed_pct          writable, 0–100
    MSV 1  mode_cmd               writable, 1=off 2=cool 3=heat 4=fan 5=auto
    BV 2   enable_cmd             writable, active=on

The simulator runs a first-order thermal model: supply air tracks the
setpoint with lag, return air tracks supply with a static offset modulated
by mode, outside air drifts on a 24-hour sine. fault_active latches when
|supply - setpoint| > 4 °C for more than 10 seconds — toggle enable_cmd
off/on to clear.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import socket
import time

from bacpypes3.app import Application
from bacpypes3.local.analog import AnalogValueObject as _AVO
from bacpypes3.local.binary import BinaryValueObject as _BVO
from bacpypes3.local.cmd import Commandable
from bacpypes3.object import (
    AnalogInputObject,
    MultiStateValueObject,
)


HVAC_ID         = os.getenv("HVAC_ID", "hvac-1")
DEVICE_INSTANCE = int(os.getenv("BACPYPES_DEVICE_INSTANCE", "3001"))
TICK_SECS       = float(os.getenv("TICK_SECONDS", "1"))


class CommandableAV(Commandable, _AVO):
    pass


class CommandableBV(Commandable, _BVO):
    pass


class CommandableMSV(Commandable, MultiStateValueObject):
    pass


def primary_ip() -> str:
    """Best-effort detection of this container's outbound IP. bacpypes3 uses
    this to derive the broadcast address; ifaddr auto-detection works on a
    single-interface container but UDP-connect is more deterministic."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


def build_args() -> argparse.Namespace:
    """Construct the namespace Application.from_args expects without going
    through the CLI — every field is env-driven inside the container."""
    return argparse.Namespace(
        name=os.getenv("BACPYPES_DEVICE_NAME", HVAC_ID),
        instance=DEVICE_INSTANCE,
        address=os.getenv("BACPYPES_DEVICE_ADDRESS") or f"{primary_ip()}/16",
        network=int(os.getenv("BACPYPES_NETWORK", "0")),
        vendoridentifier=int(os.getenv("BACPYPES_VENDOR_IDENTIFIER", "999")),
        foreign=os.getenv("BACPYPES_FOREIGN_BBMD"),
        ttl=int(os.getenv("BACPYPES_FOREIGN_TTL", "30")),
        bbmd=os.getenv("BACPYPES_BBMD_BDT"),
        debug=[],
        color=False,
        route_aware=False,
    )


def make_objects() -> dict:
    return {
        # Sensors
        "supply_air_temp_c":    AnalogInputObject(
            objectIdentifier=("analogInput", 0),
            objectName="supply_air_temp_c",
            presentValue=14.0, units="degreesCelsius",
            description="Supply air temperature leaving cooling coil"),
        "return_air_temp_c":    AnalogInputObject(
            objectIdentifier=("analogInput", 1),
            objectName="return_air_temp_c",
            presentValue=23.0, units="degreesCelsius"),
        "outside_air_temp_c":   AnalogInputObject(
            objectIdentifier=("analogInput", 2),
            objectName="outside_air_temp_c",
            presentValue=18.0, units="degreesCelsius"),
        "chilled_water_temp_c": AnalogInputObject(
            objectIdentifier=("analogInput", 3),
            objectName="chilled_water_temp_c",
            presentValue=7.0, units="degreesCelsius"),
        "fan_kw":               AnalogInputObject(
            objectIdentifier=("analogInput", 4),
            objectName="fan_kw",
            presentValue=0.0, units="kilowatts"),
        "filter_dp_pa":         AnalogInputObject(
            objectIdentifier=("analogInput", 5),
            objectName="filter_dp_pa",
            presentValue=80.0, units="pascals"),
        # Status
        "unit_running":         _BVO(
            objectIdentifier=("binaryValue", 0),
            objectName="unit_running", presentValue="inactive"),
        "fault_active":         _BVO(
            objectIdentifier=("binaryValue", 1),
            objectName="fault_active", presentValue="inactive"),
        "mode_actual":          MultiStateValueObject(
            objectIdentifier=("multiStateValue", 0),
            objectName="mode_actual", numberOfStates=4, presentValue=1,
            stateText=["off", "cooling", "heating", "fan-only"]),
        # Commandable setpoints
        "temp_setpoint_c":      CommandableAV(
            objectIdentifier=("analogValue", 0),
            objectName="temp_setpoint_c",
            presentValue=22.0, units="degreesCelsius"),
        "fan_speed_pct":        CommandableAV(
            objectIdentifier=("analogValue", 1),
            objectName="fan_speed_pct",
            presentValue=50.0, units="percent"),
        "mode_cmd":             CommandableMSV(
            objectIdentifier=("multiStateValue", 1),
            objectName="mode_cmd", numberOfStates=5, presentValue=5,
            stateText=["off", "cool", "heat", "fan", "auto"]),
        "enable_cmd":           CommandableBV(
            objectIdentifier=("binaryValue", 2),
            objectName="enable_cmd", presentValue="active"),
    }


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


async def simulate(objs: dict) -> None:
    """First-order thermal sim. Runs every TICK_SECS seconds."""
    t0 = time.monotonic()
    fault_since: float | None = None
    last_enable = "active"

    while True:
        await asyncio.sleep(TICK_SECS)
        now = time.monotonic() - t0

        enable   = str(objs["enable_cmd"].presentValue)
        setpoint = float(objs["temp_setpoint_c"].presentValue)
        fan_pct  = clamp(float(objs["fan_speed_pct"].presentValue), 0.0, 100.0)
        mode_cmd = int(objs["mode_cmd"].presentValue)
        supply   = float(objs["supply_air_temp_c"].presentValue)
        outside  = float(objs["outside_air_temp_c"].presentValue)
        filter_dp = float(objs["filter_dp_pa"].presentValue)

        # Outside air: ~24h sine, 10–28 °C.
        outside = 19.0 + 9.0 * math.sin(now / 86400.0 * 2 * math.pi)

        # Enable toggle clears fault + resets filter dp.
        if enable != last_enable:
            if enable == "active":
                fault_since = None
                objs["fault_active"].presentValue = "inactive"
                filter_dp = 80.0
            last_enable = enable

        if enable != "active":
            mode_actual = 1
            supply_target = outside
            fan_target = 0.0
        else:
            # auto = cool if outside > setpoint+1, heat if outside < setpoint-1
            if mode_cmd == 5:
                if outside > setpoint + 1:
                    mode_actual = 2
                elif outside < setpoint - 1:
                    mode_actual = 3
                else:
                    mode_actual = 4
            else:
                mode_actual = mode_cmd
            if mode_actual == 2:
                supply_target = setpoint - 2.0
            elif mode_actual == 3:
                supply_target = setpoint + 2.0
            elif mode_actual == 4:
                supply_target = outside
            else:
                supply_target = outside
            fan_target = fan_pct if mode_actual != 1 else 0.0

        # First-order lag: τ ≈ 12 s when fan is at 50%, faster at higher flow.
        tau = max(4.0, 24.0 - 0.2 * fan_target)
        alpha = TICK_SECS / tau
        supply = supply + alpha * (supply_target - supply)

        # Return tracks supply with mode-dependent offset.
        if mode_actual == 2:
            return_temp = supply + 6.0
        elif mode_actual == 3:
            return_temp = supply - 4.0
        else:
            return_temp = (supply + outside) / 2

        # Chilled water swings 6–10 °C when cooling, drifts to 18 °C otherwise.
        chw_target = 6.5 + 1.5 * math.sin(now / 30.0) if mode_actual == 2 else 18.0
        chw = float(objs["chilled_water_temp_c"].presentValue)
        chw = chw + 0.05 * (chw_target - chw)

        # Fan power: cubic relation to flow %.
        fan_kw = 6.0 * (fan_target / 100.0) ** 3

        # Filter clogs ~1 Pa/min while running.
        if mode_actual != 1:
            filter_dp += TICK_SECS / 60.0
        filter_dp = clamp(filter_dp, 80.0, 600.0)

        # Fault detection: supply persistently off setpoint while cooling/heating.
        if enable == "active" and mode_actual in (2, 3) and abs(supply - setpoint) > 4.0:
            fault_since = fault_since or now
            if now - fault_since > 10.0:
                objs["fault_active"].presentValue = "active"
        else:
            fault_since = None

        objs["supply_air_temp_c"].presentValue    = round(supply, 2)
        objs["return_air_temp_c"].presentValue    = round(return_temp, 2)
        objs["outside_air_temp_c"].presentValue   = round(outside, 2)
        objs["chilled_water_temp_c"].presentValue = round(chw, 2)
        objs["fan_kw"].presentValue               = round(fan_kw, 3)
        objs["filter_dp_pa"].presentValue         = round(filter_dp, 1)
        objs["unit_running"].presentValue         = "active" if mode_actual != 1 else "inactive"
        objs["mode_actual"].presentValue          = mode_actual

        print(f"{HVAC_ID} mode={mode_actual} "
              f"supply={supply:5.2f} set={setpoint:5.2f} return={return_temp:5.2f} "
              f"out={outside:5.2f} chw={chw:5.2f} fan_kw={fan_kw:4.2f} "
              f"dp={filter_dp:5.1f} fault={objs['fault_active'].presentValue}",
              flush=True)


def enable_debug() -> None:
    """HVAC_DEBUG=1 turns on bacpypes3's per-module debug logging so we can
    see exactly which APDUs a client (e.g. Neuron's scan) sends and how the
    stack answers. Very verbose — diagnostic use only."""
    import logging
    from bacpypes3.argparse import create_log_handler

    logging.getLogger().setLevel(logging.DEBUG)
    for name in (
        "bacpypes3.ipv4.IPv4DatagramServer",
        "bacpypes3.netservice.NetworkServiceAccessPoint",
        "bacpypes3.appservice.ApplicationServiceAccessPoint",
        "bacpypes3.app.Application",
        "bacpypes3.service.device.WhoIsIAmServices",
        "bacpypes3.service.device.DeviceObject",
        "bacpypes3.service.object.ReadWritePropertyServices",
        "bacpypes3.service.object.ReadWritePropertyMultipleServices",
    ):
        try:
            create_log_handler(name)
        except RuntimeError:
            pass  # logger not registered yet — skip


async def main() -> None:
    args = build_args()
    print(f"BACnet device: {args.name} instance={args.instance} "
          f"address={args.address}", flush=True)

    app = Application.from_args(args)
    objs = make_objects()
    for obj in objs.values():
        app.add_object(obj)

    if os.getenv("HVAC_DEBUG"):
        enable_debug()
        print("HVAC_DEBUG: bacpypes3 debug logging enabled", flush=True)

    try:
        await simulate(objs)
    finally:
        app.close()


if __name__ == "__main__":
    asyncio.run(main())
