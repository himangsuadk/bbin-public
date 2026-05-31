"""Read-only HTTP control plane (standard-library only).

Exposes the running service over HTTP using `http.server`, so it has zero
external dependencies and is guaranteed to run wherever Python does.

Endpoints
---------
GET  /health                         liveness + uptime + cycles run
GET  /plants                         configured plants
GET  /decisions                      latest decision for every plant
GET  /decisions/{plant_id}           latest full decision + trail for one plant
GET  /history/{plant_id}             recent decision summaries for one plant
GET  /approvals/{cycle_id}           current maker-checker approval record
POST /approvals/{cycle_id}           record a maker/checker acceptance
                                     body: {"actor": "...", "role": "maker"|"checker"}

Design note: every GET is read-only. The only mutating endpoint records a
maker-checker acceptance; it enforces separation of duties and never itself
transmits an order to a counterparty (there is no counterparty transport here).
The no-SCADA-egress guard and the advisory-until-authorized rule are preserved:
the API surface carries no execute scope toward any operational network.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from .engine import DEFAULT_PLANTS
from .state import ServiceState


# Route table: (method, compiled-pattern, handler-name)
_ROUTES: list[tuple[str, re.Pattern, str]] = [
    ("GET", re.compile(r"^/health/?$"), "health"),
    ("GET", re.compile(r"^/plants/?$"), "plants"),
    ("GET", re.compile(r"^/decisions/?$"), "decisions"),
    ("GET", re.compile(r"^/decisions/(?P<plant>[A-Za-z0-9_\-]+)/?$"), "decision_one"),
    ("GET", re.compile(r"^/history/(?P<plant>[A-Za-z0-9_\-]+)/?$"), "history_one"),
    ("GET", re.compile(r"^/approvals/(?P<cycle>[A-Za-z0-9\-]+)/?$"), "approval_get"),
    ("POST", re.compile(r"^/approvals/(?P<cycle>[A-Za-z0-9\-]+)/?$"), "approval_post"),
]


def make_handler(state: ServiceState) -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        server_version = "BBIN-Service/0.1"

        # -- helpers ---------------------------------------------------------
        def _send(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            for m, pat, name in _ROUTES:
                if m != method:
                    continue
                match = pat.match(self.path.split("?")[0])
                if match:
                    getattr(self, f"_h_{name}")(**match.groupdict())
                    return
            self._send(404, {"error": "not_found", "path": self.path})

        def do_GET(self) -> None:   # noqa: N802 (http.server API)
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            # Compact access log to stdout.
            print(f"[api] {self.address_string()} {fmt % args}")

        # -- handlers --------------------------------------------------------
        def _h_health(self) -> None:
            self._send(200, {
                "status": "ok",
                "service": "bbin-platform",
                "version": "0.1.0",
                "now_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "started_at": state.started_at,
                "cycles_run": state.cycles_run,
                "note": "DEMO: synthetic data, no broker/lakehouse, no legal instruments.",
            })

        def _h_plants(self) -> None:
            self._send(200, [
                {"plant_id": p.plant_id, "corridor_id": p.corridor_id,
                 "capacity_mw": p.capacity_mw, "approved_mw": p.approved_mw}
                for p in DEFAULT_PLANTS
            ])

        def _h_decisions(self) -> None:
            latest = state.all_latest()
            self._send(200, [
                {"plant_id": r["plant_id"], "cycle_id": r["cycle_id"],
                 "generated_at_utc": r["generated_at_utc"], **r["decision"]}
                for r in latest
            ])

        def _h_decision_one(self, plant: str) -> None:
            r = state.latest(plant)
            if r is None:
                self._send(404, {"error": "no_decision_yet", "plant_id": plant})
                return
            self._send(200, r)

        def _h_history_one(self, plant: str) -> None:
            self._send(200, {"plant_id": plant, "history": state.history(plant)})

        def _h_approval_get(self, cycle: str) -> None:
            rec = state.approval(cycle)
            if rec is None:
                self._send(404, {"error": "no_approval_record", "cycle_id": cycle})
                return
            self._send(200, rec)

        def _h_approval_post(self, cycle: str) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid_json"})
                return
            actor = body.get("actor")
            role = body.get("role")
            if not actor or role not in ("maker", "checker"):
                self._send(400, {"error": "require actor and role in {maker,checker}"})
                return
            try:
                rec = state.record_approval(cycle, actor, role)
            except ValueError as e:
                self._send(409, {"error": str(e)})
                return
            rec["note"] = ("Recommendation is advisory. Both distinct maker and checker "
                           "acceptances are required before it could be transmitted to a "
                           "trader in production.")
            self._send(200, rec)

    return Handler


def serve(state: ServiceState, host: str = "127.0.0.1", port: int = 8077
          ) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    return httpd
