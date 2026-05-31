"""Entrypoint for the BBIN Hydropower Platform local service (Level 2).

Starts the background forecast-cycle scheduler and the read-only HTTP control
plane, then serves until interrupted.

Run:
    $env:PYTHONPATH = "<repo>\\bbin-platform"
    python -m service                       # 15-min cadence, port 8077
    python -m service --interval 10 --port 8077 --paths 20000   # fast demo

Then, in another shell:
    curl http://127.0.0.1:8077/health
    curl http://127.0.0.1:8077/decisions
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .api import serve
from .scheduler import ForecastScheduler
from .state import ServiceState


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="service", description="BBIN local service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--interval", type=float, default=900.0,
                        help="seconds between forecast cycles (default 900 = 15 min)")
    parser.add_argument("--paths", type=int, default=100_000,
                        help="Monte Carlo paths per cycle (lower = faster demo)")
    args = parser.parse_args(argv)

    state = ServiceState()
    state.started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    scheduler = ForecastScheduler(state, interval_s=args.interval, n_paths=args.paths)
    httpd = serve(state, host=args.host, port=args.port)

    print("BBIN HYDROPOWER PLATFORM - LOCAL SERVICE (Level 2)")
    print(f"  control plane : http://{args.host}:{args.port}")
    print(f"  cycle cadence : {args.interval:.0f}s   monte-carlo paths: {args.paths:,}")
    print("  endpoints     : /health  /plants  /decisions  /decisions/{plant}")
    print("                  /history/{plant}  /approvals/{cycle_id}")
    print("  NOTE: DEMO - synthetic data, no broker/lakehouse, no legal instruments.")
    print("  Press Ctrl+C to stop.\n")

    scheduler.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down ...")
    finally:
        scheduler.stop()
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
