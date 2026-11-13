"""Replayable mod lifecycle sequences and resource-reclamation checks.

A sequence is a list of plain steps executed against one Host on a minimal
level. The same sequence drives both the headless CLI checks and the
graphical mod-host panel, so the GUI cannot implement its own lifecycle
semantics: every action goes through the host kernel.
"""

import gc
import json
import os
import weakref

from .errors import LoadError
from .host import Host
from .limits import Limits
from .loader import load_mod


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SEQUENCE_PATH = os.path.join(ROOT, "sequences", "lifecycle.json")


def _catalog_path(name, base):
    if base == "tests/fixtures":
        return os.path.join(ROOT, "tests", "fixtures", name)
    return os.path.join(ROOT, "mods", name)


class ReclamationProbe:
    """Weak-reference witness that an unloaded generation was actually
    released instead of merely being marked closed."""

    def __init__(self):
        self._refs = {}

    def track(self, label, obj):
        try:
            ref = weakref.ref(obj)
        except TypeError:
            return
        self._refs[label] = ref

    def report(self):
        gc.collect()
        gc.collect()
        alive = {label for label, ref in self._refs.items() if ref() is not None}
        return {
            "tracked": sorted(self._refs),
            "alive": sorted(alive),
            "reclaimed": sorted(set(self._refs) - alive),
            "ok": not alive,
        }


def capture_generation_probe(host, mod_id):
    """Snapshot weak references for one loaded mod generation."""
    probe = ReclamationProbe()
    ctx = host.mods[mod_id]
    api = host.apis[mod_id]
    namespace = host.namespaces[mod_id]
    probe.track(f"{mod_id}:context", ctx)
    probe.track(f"{mod_id}:api", api)
    register_fn = namespace.get("register")
    if register_fn is not None:
        # A function's __globals__ is the module namespace; its death
        # proves the whole script namespace was released.
        probe.track(f"{mod_id}:namespace", register_fn)
    for event, handler in tuple(ctx.storage.get("handlers", {}).items()):
        probe.track(f"{mod_id}:handler:{event}", handler)
    return probe


def assert_reclaimed(host, probe):
    report = probe.report()
    structural = structural_reclamation(host)
    if not report["ok"] or not structural["ok"]:
        raise AssertionError(
            f"reclamation failed: weakref alive={report['alive']} "
            f"structural={structural['errors']}"
        )
    return report, structural


def structural_reclamation(host):
    """Host-side invariants that must hold for every unloaded mod."""
    errors = []
    for tombstone in host.retired:
        mid = tombstone["mod_id"]
        if mid in host.mods:
            errors.append(f"{mid} still registered after unload")
        if mid in host.apis:
            errors.append(f"{mid} still has an API handle after unload")
        if mid in host.namespaces:
            errors.append(f"{mid} still holds its namespace after unload")
        if any(c.mod_id == mid for c in host.deferred):
            errors.append(f"{mid} still has deferred callbacks queued")
        if not tombstone["retired"]:
            errors.append(f"{mid} tombstone not marked retired")
        if tombstone["handler_events"]:
            errors.append(f"{mid} tombstone still exposes handlers {tombstone['handler_events']}")
    for mod_id, ctx in host.mods.items():
        if not ctx.retired and not ctx.storage.get("handlers"):
            errors.append(f"{mod_id} has no subscriptions while still registered")
    return {"ok": not errors, "errors": errors}


def load_for_step(host, step):
    name = step["name"]
    base = step.get("base", "mods")
    overrides = {}
    if "version" in step:
        overrides["version"] = step["version"]
    if "entry" in step:
        overrides["entry"] = step["entry"]
    mod_id, namespace, manifest = load_mod(_catalog_path(name, base), overrides=overrides)
    host.register(mod_id, namespace, manifest)
    return mod_id


def replay_sequence(host, sequence, probes=None):
    """Run one replayable sequence and return a trace of kernel results."""
    probes = probes if probes is not None else {}
    trace = []
    for step in sequence:
        action = step["action"]
        record = {"step": step}
        try:
            if action == "load":
                mod_id = load_for_step(host, step)
                probe = capture_generation_probe(host, mod_id)
                probes.setdefault((mod_id, host.mods[mod_id].generation), probe)
                record["result"] = {"loaded": mod_id,
                                    "generation": host.mods[mod_id].generation}
            elif action == "begin_level":
                host.begin_level()
                record["result"] = {"epoch": host.epoch}
            elif action == "score":
                gained = host.award_score(step["amount"])
                record["result"] = {"gained": gained, "score": host.engine.state.score}
            elif action == "tick":
                host.tick()
                record["result"] = {"tick": host.engine.state.tick,
                                    "gems": host.engine.state.gems}
            elif action == "death":
                record["result"] = {"revived": host.die()}
            elif action == "enable":
                record["result"] = {"enabled": host.enable(step["mod"])}
            elif action == "disable":
                host.disable(step["mod"])
                record["result"] = {"enabled": host.mods[step["mod"]].enabled}
            elif action == "unload":
                record["result"] = host.unload(step["mod"])
            elif action == "switch_scene":
                host.switch_scene(step["scene"])
                record["result"] = {"scene": host.scene_id, "epoch": host.epoch}
            elif action == "check_reclaim":
                key = next((k for k in probes if k[0] == step["mod"]), None)
                probe = probes[key] if key is not None else None
                if probe is None:
                    raise KeyError(f"no probe for {step['mod']}")
                report, structural = assert_reclaimed(host, probe)
                record["result"] = {"weakref": report, "structural": structural}
            else:
                raise LoadError(f"unknown lifecycle step {action!r}")
        except Exception as exc:  # failures are recorded, never silently swallowed
            record["error"] = {"kind": type(exc).__name__, "message": str(exc)}
            trace.append(record)
            raise
        trace.append(record)
    return trace


def load_sequences(path=None):
    with open(path or DEFAULT_SEQUENCE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def run_all(limits=None, path=None):
    """Replay every named sequence on fresh minimal-level hosts."""
    results = []
    for case in load_sequences(path):
        host = Host("minimal", limits=limits or Limits())
        try:
            trace = replay_sequence(host, case["steps"])
            results.append({
                "label": case["label"],
                "ok": all("error" not in entry for entry in trace),
                "trace": trace,
                "report": host.report(),
            })
        finally:
            host.close()
    return results
