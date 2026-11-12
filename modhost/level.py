"""Deterministic level script and validation.

The same fixed scenario runs under every mod configuration. The scenario
is explicit gameplay (ticks, scoring, deaths, payment), not a list of mod
filenames: mods only react through declared event hooks.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Expectation:
    label: str
    predicate: object


# Fixed scenario: 6 ticks, two scoring waves, one fatal death on tick 4.
SCENARIO = [
    ("tick", None),
    ("score", 100),
    ("tick", None),
    ("score", 50),
    ("tick", None),
    ("death", None),
    ("tick", None),
    ("score", 200),
    ("tick", None),
    ("tick", None),
]


def run_scenario(host):
    host.begin_level()
    try:
        for kind, arg in SCENARIO:
            if kind == "tick":
                host.tick()
            elif kind == "score":
                host.award_score(arg)
            elif kind == "death":
                host.die()
        return host.end_level()
    finally:
        host.close()


# Invariants that must hold under every configuration.
def hard_invariants(report):
    errors = []
    if report["gems"] < 0:
        errors.append("gems went negative: settlement was not atomic")
    if report["resource_cost"] < 1:
        errors.append("resource cost dropped below 1")
    if report["multiplier"] < 1.0:
        errors.append("multiplier dropped below baseline 1.0")
    state = report
    if state["spawned_count"] != len(state["objects"]):
        errors.append("spawned_count does not match object list")
    for mid, ctx in report["mods"].items():
        if ctx["suspended"] and ctx["violation"] is None:
            errors.append(f"mod {mid} suspended without retained violation context")
    return errors


def validate(report, expectations=None):
    errors = hard_invariants(report)
    for expectation in expectations or ():
        if not expectation.predicate(report):
            errors.append(f"expectation failed: {expectation.label}")
    return {
        "ok": not errors,
        "errors": errors,
        "score": report["score"],
        "gems": report["gems"],
        "lives": report["lives"],
        "multiplier": report["multiplier"],
        "resource_cost": report["resource_cost"],
        "spawned": report["spawned_count"],
        "violations": report["violations"],
    }
