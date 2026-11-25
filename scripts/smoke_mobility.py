"""Quick check that the Mobility API answers (read-only). Follows USE_MOCKS.

    python scripts/smoke_mobility.py

Logs in, counts the units and shows the latest position of the first 3 units:
Name, LastReportedTimeUTC, Ignition and the number of sensor readings. Nothing else.
"""

import logging
import sys

import structlog

from connectors.mobility import MobilityClient, MobilityError, get_mobility_client

SAMPLE_UNITS = 3


def run(client: MobilityClient) -> list[str]:
    """The lines to print."""
    client.authenticate()
    units = client.list_units()
    lines = [f"Units: {len(units)}"]
    for unit in units[:SAMPLE_UNITS]:
        for lp in client.latest_positions(unit_uid=unit.uid):
            ignition = lp.position.ignition if lp.position else None
            lines.append(
                f"{lp.name} | {lp.last_reported_time_utc} | {ignition} | "
                f"{len(lp.sensor_readings)} sensor readings"
            )
    return lines


def main() -> int:
    # Hide the per-request log lines so only the results are printed.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # Greek names must not crash the console
    try:
        with get_mobility_client() as client:
            lines = run(client)
    except (MobilityError, RuntimeError) as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
