"""Graphical mod-host panel.

The panel is a thin view: every button posts an action to this server, and
the server drives the real Host kernel. Controls, level effects and failure
feedback are all computed by the host and rendered back; the browser holds
no game rules of its own.

Entry point (documented in README.md):

    python3 -I -m modhost.panel --port 8765
    # then open http://127.0.0.1:8765/
"""

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ModError, Violation
from .host import Host
from .lifecycle import (
    DEFAULT_SEQUENCE_PATH,
    capture_generation_probe,
    load_sequences,
    replay_sequence,
    structural_reclamation,
)
from .limits import Limits
from .loader import load_mod


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")

# The catalog the kernel exposes to the panel. The browser cannot request
# arbitrary paths; only these host-approved mod cases are loadable.
CATALOG = {
    "greedy": {"path": os.path.join(ROOT, "mods", "greedy"), "base": "mods"},
    "greedy_v2": {"path": os.path.join(ROOT, "mods", "greedy"), "base": "mods",
                  "overrides": {"version": "2.0.0", "entry": "mod_v2.py"}},
    "thrifty": {"path": os.path.join(ROOT, "mods", "thrifty"), "base": "mods"},
    "defer_keeper": {"path": os.path.join(ROOT, "tests", "fixtures", "defer_keeper"),
                     "base": "tests/fixtures"},
    "crash": {"path": os.path.join(ROOT, "tests", "fixtures", "crash"),
              "base": "tests/fixtures"},
    "spawn_bomb": {"path": os.path.join(ROOT, "tests", "fixtures", "spawn_bomb"),
                   "base": "tests/fixtures"},
}


def _to_jsonable(value):
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # FrozenView and anything else: fall back to its plain representation.
    if hasattr(value, "to_dict"):
        return _to_jsonable(value.to_dict())
    return repr(value)


class PanelSession:
    """Owns one Host and serializes every action through the kernel."""

    def __init__(self, limits=None):
        self.lock = threading.Lock()
        self.host = Host("minimal", limits=limits or Limits())
        self.host.begin_level()
        self.probes = {}
        self.feedback = []

    # --- actions ---------------------------------------------------------

    def act(self, payload):
        action = str(payload.get("action", ""))
        with self.lock:
            try:
                result = self._dispatch_action(action, payload)
                self.feedback.append({"level": "info", "action": action, "result": result})
            except (ModError, Violation, KeyError, ValueError) as exc:
                # Failures are fed back to the host's own ledger, never
                # turned into a fake successful UI state.
                entry = {"level": "error", "action": action,
                         "kind": type(exc).__name__, "message": str(exc)}
                self.feedback.append(entry)
                return {"ok": False, "error": entry, "state": self.view()}
            return {"ok": True, "result": _to_jsonable(result), "state": self.view()}

    def _dispatch_action(self, action, payload):
        host = self.host
        if action == "load":
            name = payload["name"]
            spec = CATALOG[name]
            mod_id, namespace, manifest = load_mod(spec["path"], overrides=spec.get("overrides"))
            host.register(mod_id, namespace, manifest)
            probe = capture_generation_probe(host, mod_id)
            self.probes[(mod_id, host.mods[mod_id].generation)] = probe
            return {"loaded": mod_id, "generation": host.mods[mod_id].generation,
                    "version": host.mods[mod_id].version}
        if action == "enable":
            return {"enabled": host.enable(payload["mod"])}
        if action == "disable":
            host.disable(payload["mod"])
            return {"enabled": host.mods[payload["mod"]].enabled}
        if action == "unload":
            return host.unload(payload["mod"])
        if action == "tick":
            host.tick()
            return {"tick": host.engine.state.tick, "gems": host.engine.state.gems}
        if action == "score":
            gained = host.award_score(int(payload.get("amount", 100)))
            return {"gained": gained, "score": host.engine.state.score}
        if action == "death":
            return {"revived": host.die()}
        if action == "switch_scene":
            host.switch_scene(payload.get("scene", "minimal_b"))
            return {"scene": host.scene_id, "epoch": host.epoch}
        if action == "reset_level":
            host.begin_level()
            return {"epoch": host.epoch}
        if action == "run_sequence":
            return self._run_sequence(payload.get("label"), payload.get("path"))
        raise KeyError(f"unknown panel action {action!r}")

    def _run_sequence(self, wanted, path):
        cases = load_sequences(path or DEFAULT_SEQUENCE_PATH)
        chosen = next((c for c in cases if c["label"] == wanted), cases[0])
        runner_host = Host("minimal", limits=self.host.limits)
        probes = {}
        try:
            trace = replay_sequence(runner_host, chosen["steps"], probes=probes)
            structural = structural_reclamation(runner_host)
            return {
                "label": chosen["label"],
                "trace": _to_jsonable(trace),
                "structural": structural,
                "report": _to_jsonable(runner_host.report()),
            }
        finally:
            runner_host.close()

    # --- view ------------------------------------------------------------

    def view(self):
        host = self.host
        state = host.engine.state
        return _to_jsonable({
            "scene_id": host.scene_id,
            "epoch": host.epoch,
            "catalog": sorted(CATALOG),
            "controls": self._controls(),
            "state": {
                "tick": state.tick,
                "score": state.score,
                "multiplier": state.multiplier,
                "lives": state.lives,
                "gems": state.gems,
                "resource_cost": state.resource_cost,
                "spawned_count": state.spawned_count,
                "event_count": state.event_count,
            },
            "mods": {mid: ctx.snapshot() for mid, ctx in sorted(host.mods.items())},
            "deferred": host.report()["deferred_queued"],
            "stale_drops": host.stale_drops,
            "retired": host.retired,
            "last_settlement": host.last_settlement,
            "violations": host.report()["violations"],
            "feedback": self.feedback[-25:],
        })

    def _controls(self):
        # The kernel declares which controls exist and whether they apply
        # to the current registry; the page just renders the list.
        mod_ids = sorted(self.host.mods)
        return [
            {"id": "load", "label": "Load catalog mod", "needs": "catalog"},
            {"id": "score", "label": "Trigger on_score (+100)"},
            {"id": "tick", "label": "Trigger on_tick"},
            {"id": "death", "label": "Trigger on_death"},
            {"id": "enable", "label": "Enable", "applies_to": mod_ids},
            {"id": "disable", "label": "Disable", "applies_to": mod_ids},
            {"id": "unload", "label": "Unload & release", "applies_to": mod_ids},
            {"id": "switch_scene", "label": "Switch host scene"},
            {"id": "run_sequence", "label": "Replay lifecycle case",
             "cases": [c["label"] for c in load_sequences()]},
        ]


class PanelHandler(BaseHTTPRequestHandler):
    session = None

    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(PANEL_HTML, "rb") as handle:
                self._send(200, handle.read(), "text/html; charset=utf-8")
            return
        if self.path == "/api/state":
            self._send(200, self.session.view())
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/api/action":
            self._send(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send(400, {"ok": False, "error": {"kind": "JSONDecodeError",
                                                     "message": str(exc)}})
            return
        try:
            response = self.session.act(payload)
        except Exception as exc:  # defensive: surface, never fake success
            self._send(500, {"ok": False,
                             "error": {"kind": type(exc).__name__, "message": str(exc)}})
            return
        self._send(200 if response["ok"] else 409, response)


def serve(port=8765, bind="127.0.0.1"):
    PanelHandler.session = PanelSession()
    server = ThreadingHTTPServer((bind, port), PanelHandler)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Graphical mod-host panel")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args(argv)
    server = serve(args.port, args.bind)
    print(f"mod-host panel: open http://{args.bind}:{args.port}/ (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
