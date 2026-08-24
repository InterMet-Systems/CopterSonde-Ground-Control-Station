#!/usr/bin/env python3
"""
req50_loss_of_control_sim.py

Desktop MAVLink test vehicle for CopterSonde GCS SoW requirement #50.

The malformed cross-reference in requirement #50 appears to refer to #42:
#42 automatically asserts OPEN_DRONE_ID_SELF_ID when SYS_STATUS reports an
unhealthy enabled/present sensor or AHRS, and clears it only after 10 seconds
of continuously healthy reports. Requirement #40 defines the SELF_ID payload.

This simulator:
  1. Pretends to be an ArduCopter over MAVLink 2.
  2. Waits until it sees a GCS heartbeat, so the test does not start while
     CGCS is still launching.
  3. Sends healthy SYS_STATUS messages for a short baseline period.
  4. Simulates a loss-of-control condition by marking the AHRS unhealthy while
     leaving it present and enabled.
  5. Restores AHRS health and continues sending healthy status long enough to
     exercise the 10-second recovery rule.
  6. Listens for OPEN_DRONE_ID_SELF_ID from the GCS and performs basic checks
     on assertion, payload, persistence, and clearing.

Typical use:
    python req50_loss_of_control_sim.py

Then connect CGCS using the same local UDP setup as fake_coptersonde.py:
    simulator: udpout:127.0.0.1:14550
    CGCS:      udpin:0.0.0.0:14550  (or the HereLink Hotspot preset)

An alternate MAVLink connection string may be supplied positionally:
    python req50_loss_of_control_sim.py udpout:127.0.0.1:14551

Exit status:
    0 = test passed
    1 = test completed but one or more checks failed
    2 = startup/configuration error

Ctrl-C stops the test without reporting PASS/FAIL.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# MAVLink 2 framing, matching HereLink/ArduPilot. Must be set before pymavlink.
os.environ.setdefault("MAVLINK20", "1")

try:
    from pymavlink import mavutil
except ImportError as exc:  # pragma: no cover - depends on local environment
    print("ERROR: pymavlink is required to run this simulator.", file=sys.stderr)
    print("Install/use the same pymavlink environment as CGCS.", file=sys.stderr)
    raise SystemExit(2) from exc


DEFAULT_CONN = "udpout:127.0.0.1:14550"
SYSID = 1
COMPID = 1
TICK_S = 0.1
STATUS_INTERVAL_S = 1.0

BASELINE_S = 4.0
FAULT_S = 8.0
HEALTHY_CLEAR_S = 10.0  # SoW #42 / CGCS behavior
CLEAR_GRACE_S = 1.5  # scheduling/network tolerance after the 10 s boundary
QUIET_CHECK_S = 4.0

BASE_LAT = 35.2226
BASE_LON = -97.4395
BASE_ALT_AMSL = 357.0

_M = mavutil.mavlink

# Prefer the explicit AHRS bit. The numeric fallback is the MAVLink definition
# (1 << 21) and keeps the simulator usable with older generated dialects that
# omit the symbolic constant.
AHRS_BIT = getattr(_M, "MAV_SYS_STATUS_SENSOR_AHRS", 1 << 21)

# A realistic subset of ArduCopter status bits. Resolve each symbol lazily so
# older pymavlink builds can still run the test.
def _sensor_bit(name: str, fallback: int) -> int:
    return int(getattr(_M, name, fallback))


SENSORS_PRESENT_ENABLED = (
    _sensor_bit("MAV_SYS_STATUS_SENSOR_3D_GYRO", 1 << 0)
    | _sensor_bit("MAV_SYS_STATUS_SENSOR_3D_ACCEL", 1 << 1)
    | _sensor_bit("MAV_SYS_STATUS_SENSOR_3D_MAG", 1 << 2)
    | _sensor_bit("MAV_SYS_STATUS_SENSOR_ABSOLUTE_PRESSURE", 1 << 3)
    | _sensor_bit("MAV_SYS_STATUS_SENSOR_GPS", 1 << 5)
    | _sensor_bit("MAV_SYS_STATUS_SENSOR_RC_RECEIVER", 1 << 16)
    | _sensor_bit("MAV_SYS_STATUS_SENSOR_BATTERY", 1 << 17)
    | AHRS_BIT
)


@dataclass
class TestResults:
    self_id_times: list[float] = field(default_factory=list)
    payload_errors: list[str] = field(default_factory=list)
    pre_fault_count: int = 0
    fault_count: int = 0
    recovery_hold_count: int = 0
    post_clear_count: int = 0
    gcs_system_id: Optional[int] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulate an AHRS loss-of-control condition for CGCS Remote ID testing."
    )
    parser.add_argument(
        "connection_string",
        nargs="?",
        default=DEFAULT_CONN,
        help=f"pymavlink connection string (default: {DEFAULT_CONN})",
    )
    parser.add_argument(
        "--baseline",
        type=float,
        default=BASELINE_S,
        help=f"healthy seconds before the fault (default: {BASELINE_S:g})",
    )
    parser.add_argument(
        "--fault-duration",
        type=float,
        default=FAULT_S,
        help=f"seconds to report AHRS unhealthy (default: {FAULT_S:g})",
    )
    parser.add_argument(
        "--clear-delay",
        type=float,
        default=HEALTHY_CLEAR_S,
        help=(
            "expected healthy hold time before SELF_ID clears "
            f"(default: {HEALTHY_CLEAR_S:g})"
        ),
    )
    parser.add_argument(
        "--quiet-check",
        type=float,
        default=QUIET_CHECK_S,
        help=f"seconds to watch for packets after expected clear (default: {QUIET_CHECK_S:g})",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="generate the condition and print received messages without PASS/FAIL checks",
    )
    return parser.parse_args()


def _decode_char_field(value) -> str:
    """Best-effort conversion of a MAVLink char/uint8 array to text."""
    if isinstance(value, str):
        return value.split("\x00", 1)[0]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).split(b"\x00", 1)[0].decode("ascii", "replace")
    try:
        raw = bytes(value)
    except (TypeError, ValueError):
        return str(value)
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace")


def send_heartbeat(conn) -> None:
    conn.mav.heartbeat_send(
        _M.MAV_TYPE_QUADROTOR,
        _M.MAV_AUTOPILOT_ARDUPILOTMEGA,
        _M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        5,  # ArduCopter LOITER custom_mode
        _M.MAV_STATE_ACTIVE,
    )


def send_sys_status(conn, unhealthy: bool) -> None:
    present = SENSORS_PRESENT_ENABLED
    enabled = SENSORS_PRESENT_ENABLED
    health = SENSORS_PRESENT_ENABLED
    if unhealthy:
        # This is the actual stimulus under test: AHRS remains present/enabled,
        # but is removed from the health mask.
        health &= ~AHRS_BIT

    conn.mav.sys_status_send(
        present,
        enabled,
        health,
        350,     # load, 0.1%
        25200,   # battery voltage, mV
        60,      # current, cA
        95,      # battery remaining, %
        0,       # drop_rate_comm
        0,       # errors_comm
        0, 0, 0, 0,  # errors_count1..4
    )


def send_position(conn, boot_ms: int) -> None:
    """Enough position telemetry to make the fake vehicle look normal in CGCS."""
    lat = int(round(BASE_LAT * 1e7))
    lon = int(round(BASE_LON * 1e7))
    alt_mm = int(BASE_ALT_AMSL * 1000)

    conn.mav.global_position_int_send(
        boot_ms,
        lat,
        lon,
        alt_mm,
        0,      # relative_alt, mm
        0, 0, 0,
        0,      # hdg unknown/zero is fine for this test
    )
    conn.mav.gps_raw_int_send(
        boot_ms * 1000,
        3,      # 3D fix
        lat,
        lon,
        alt_mm,
        80,     # eph = 0.80 HDOP
        100,    # epv
        0,      # vel
        0,      # cog
        14,     # satellites_visible
    )


def inspect_self_id(msg, results: TestResults, rel_t: float, phase: str) -> None:
    results.self_id_times.append(rel_t)

    if phase == "baseline":
        results.pre_fault_count += 1
    elif phase == "fault":
        results.fault_count += 1
    elif phase == "recovery-hold":
        results.recovery_hold_count += 1
    elif phase == "post-clear":
        results.post_clear_count += 1

    desc_type = getattr(msg, "description_type", None)
    description = _decode_char_field(getattr(msg, "description", ""))
    target_system = getattr(msg, "target_system", None)
    target_component = getattr(msg, "target_component", None)

    print(
        f"[test +{rel_t:5.1f}s] RX OPEN_DRONE_ID_SELF_ID  "
        f"phase={phase}  target={target_system}/{target_component}  "
        f"type={desc_type}  description={description!r}"
    )

    if desc_type != 1:
        results.payload_errors.append(
            f"description_type was {desc_type!r}, expected 1"
        )
    if description != "system unhealthy":
        results.payload_errors.append(
            f"description was {description!r}, expected 'system unhealthy'"
        )
    if target_system != 13:
        results.payload_errors.append(
            f"target_system was {target_system!r}, expected 13"
        )
    if target_component != 0:
        results.payload_errors.append(
            f"target_component was {target_component!r}, expected 0"
        )


def phase_for(rel_t: float, baseline: float, fault_duration: float,
              expected_clear_rel: Optional[float]) -> str:
    if rel_t < baseline:
        return "baseline"
    if rel_t < baseline + fault_duration:
        return "fault"
    if expected_clear_rel is None or rel_t < expected_clear_rel + CLEAR_GRACE_S:
        return "recovery-hold"
    return "post-clear"


def evaluate(results: TestResults, baseline: float, fault_duration: float,
             expected_clear_rel: float) -> list[str]:
    failures: list[str] = []

    if results.pre_fault_count:
        failures.append(
            f"received {results.pre_fault_count} SELF_ID packet(s) before the fault"
        )

    if results.fault_count == 0:
        failures.append("no SELF_ID packet was received while AHRS was unhealthy")

    # At 1/2 Hz over the default 8 s fault we expect several packets. Requiring
    # two is intentionally tolerant of startup/scheduling jitter and UDP loss.
    if fault_duration >= 4.0 and results.fault_count < 2:
        failures.append(
            f"only {results.fault_count} SELF_ID packet(s) arrived during the fault; expected at least 2"
        )

    if results.recovery_hold_count == 0:
        failures.append(
            "SELF_ID did not persist into the healthy recovery window; #42 requires a 10 s hold"
        )

    if results.post_clear_count:
        failures.append(
            f"received {results.post_clear_count} SELF_ID packet(s) after the expected 10 s healthy clear"
        )

    failures.extend(dict.fromkeys(results.payload_errors))

    # Check approximate 1/2 Hz cadence only where consecutive packets exist.
    intervals = [b - a for a, b in zip(results.self_id_times, results.self_id_times[1:])]
    if intervals:
        # Ignore one boundary interval that can be shortened by immediate
        # assertion. All other intervals should be around 2 s; use a generous
        # desktop/UDP tolerance.
        regular = [dt for dt in intervals if dt >= 0.75]
        if regular and not any(1.2 <= dt <= 2.8 for dt in regular):
            failures.append(
                "SELF_ID cadence did not show the expected ~2 s (1/2 Hz) interval"
            )

    # Diagnostic only: expected_clear_rel is passed in so the summary can
    # report the intended boundary even if there were no packets near it.
    _ = baseline, expected_clear_rel
    return failures


def main() -> int:
    args = parse_args()

    if args.baseline < 0 or args.fault_duration <= 0 or args.clear_delay < 0 or args.quiet_check < 0:
        print("ERROR: timing arguments must be non-negative, and fault duration must be > 0.", file=sys.stderr)
        return 2

    print(f"Requirement #50 loss-of-control simulator -> {args.connection_string}")
    print(f"Fake vehicle: sysid={SYSID}, compid={COMPID}, MAVLink2")
    print(f"Fault stimulus: AHRS unhealthy bit 0x{AHRS_BIT:08X}")
    print("Waiting for a GCS heartbeat; healthy telemetry is sent while waiting.")
    print("Ctrl-C to stop.\n")

    try:
        conn = mavutil.mavlink_connection(
            args.connection_string,
            source_system=SYSID,
            source_component=COMPID,
        )
    except Exception as exc:
        print(f"ERROR: could not open MAVLink connection: {exc}", file=sys.stderr)
        return 2

    results = TestResults()
    wall_start = time.monotonic()
    test_start: Optional[float] = None
    last_status_send = -1e9
    last_heartbeat_send = -1e9
    last_fault_status_wall: Optional[float] = None
    expected_clear_rel: Optional[float] = None
    announced_phase: Optional[str] = None
    n = 0

    try:
        while True:
            now = time.monotonic()
            boot_ms = int((now - wall_start) * 1000)

            if test_start is None:
                rel_t = 0.0
                current_phase = "waiting"
                unhealthy = False
            else:
                rel_t = now - test_start
                fault_end = args.baseline + args.fault_duration
                unhealthy = args.baseline <= rel_t < fault_end

                if expected_clear_rel is None:
                    current_phase = phase_for(
                        rel_t, args.baseline, args.fault_duration, None
                    )
                else:
                    current_phase = phase_for(
                        rel_t, args.baseline, args.fault_duration, expected_clear_rel
                    )

            if current_phase != announced_phase:
                announced_phase = current_phase
                if current_phase == "baseline":
                    print(f"[test +{rel_t:5.1f}s] BASELINE: AHRS healthy")
                elif current_phase == "fault":
                    print(
                        f"[test +{rel_t:5.1f}s] LOSS OF CONTROL: AHRS marked UNHEALTHY"
                    )
                elif current_phase == "recovery-hold":
                    print(
                        f"[test +{rel_t:5.1f}s] RECOVERY: AHRS healthy again; "
                        f"SELF_ID should remain asserted for ~{args.clear_delay:g}s"
                    )
                elif current_phase == "post-clear":
                    print(
                        f"[test +{rel_t:5.1f}s] POST-CLEAR: SELF_ID should now be OFF "
                        f"(includes {CLEAR_GRACE_S:g}s scheduling tolerance)"
                    )

            # 1 Hz heartbeat, status, and basic position telemetry.
            if now - last_heartbeat_send >= STATUS_INTERVAL_S:
                send_heartbeat(conn)
                last_heartbeat_send = now

            if now - last_status_send >= STATUS_INTERVAL_S:
                send_sys_status(conn, unhealthy=unhealthy)
                send_position(conn, boot_ms)
                last_status_send = now

                if test_start is not None and unhealthy:
                    last_fault_status_wall = now
                elif (
                    test_start is not None
                    and not unhealthy
                    and rel_t >= args.baseline + args.fault_duration
                    and expected_clear_rel is None
                    and last_fault_status_wall is not None
                ):
                    # CGCS starts the 10 s clear timer at its last received bad
                    # SYS_STATUS. Use the matching simulator transmission time.
                    expected_clear_rel = (
                        last_fault_status_wall - test_start + args.clear_delay
                    )
                    print(
                        f"[test +{rel_t:5.1f}s] Last bad SYS_STATUS was at "
                        f"+{last_fault_status_wall - test_start:.1f}s; expected auto-clear "
                        f"near +{expected_clear_rel:.1f}s"
                    )

            # Drain packets from the GCS. This is also how the prior
            # fake_coptersonde.py confirms the two-way UDP link.
            for _ in range(200):
                try:
                    msg = conn.recv_match(blocking=False)
                except OSError:
                    # Windows may surface ICMP Port Unreachable on the next
                    # recvfrom() when CGCS is not listening yet. The socket is
                    # still usable, so keep going.
                    continue

                if msg is None:
                    break

                msg_type = msg.get_type()
                src_system = msg.get_srcSystem()

                if (
                    test_start is None
                    and msg_type == "HEARTBEAT"
                    and src_system != SYSID
                ):
                    results.gcs_system_id = src_system
                    test_start = time.monotonic()
                    announced_phase = None
                    print(
                        f"GCS link confirmed (sysid {src_system}). Starting test timeline now.\n"
                    )
                    continue

                if test_start is None:
                    continue

                if msg_type == "OPEN_DRONE_ID_SELF_ID":
                    rx_rel = time.monotonic() - test_start
                    rx_phase = phase_for(
                        rx_rel,
                        args.baseline,
                        args.fault_duration,
                        expected_clear_rel,
                    )
                    inspect_self_id(msg, results, rx_rel, rx_phase)

            if test_start is not None and expected_clear_rel is not None:
                rel_t = time.monotonic() - test_start
                end_rel = expected_clear_rel + CLEAR_GRACE_S + args.quiet_check
                if rel_t >= end_rel:
                    print("\n--- Requirement #50 test summary ---")
                    print(f"SELF_ID before fault:       {results.pre_fault_count}")
                    print(f"SELF_ID during fault:       {results.fault_count}")
                    print(f"SELF_ID during 10 s hold:   {results.recovery_hold_count}")
                    print(f"SELF_ID after expected off: {results.post_clear_count}")

                    if args.no_verify:
                        print("Verification disabled (--no-verify). Stimulus cycle complete.")
                        return 0

                    failures = evaluate(
                        results,
                        args.baseline,
                        args.fault_duration,
                        expected_clear_rel,
                    )
                    if failures:
                        print("\nFAIL")
                        for failure in failures:
                            print(f"  - {failure}")
                        return 1

                    print("\nPASS")
                    print(
                        "CGCS asserted the Remote ID SELF_ID emergency message during "
                        "the simulated AHRS failure and stopped it after recovery."
                    )
                    return 0

            # Absolute-ish pacing without depending on tick count for test timing.
            n += 1
            target = wall_start + n * TICK_S
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    except KeyboardInterrupt:
        print("\nStopped by user.")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
