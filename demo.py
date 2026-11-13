"""Runnable demonstration of the mod host on one fixed level.

Run:  python3 -I demo.py

Shows, in order:
  1. baseline rules with no mods
  2. the two real mods separately and combined (deterministic policy)
  3. mid-level enable/disable with rules changing immediately
  4. illegal mod rejection and runtime limit interruptions, each with
     the retained ModContext proving what happened
  5. recovery: a suspended mod can be readmitted, history stays
  6. unload releases the generation; same-name reinstall gets a new one
"""

import gc
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modhost import Host, load_mod, run_scenario, validate
from modhost.errors import LoadError, StaleGeneration
from modhost.limits import Limits
from modhost.lifecycle import capture_generation_probe, structural_reclamation

ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def banner(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(tag, report):
    result = validate(report)
    print(
        f"{tag:24s} score={report['score']:5d}  gems={report['gems']:3d}  "
        f"lives={report['lives']}  mult={report['multiplier']}  "
        f"cost={report['resource_cost']}  spawns={report['spawned_count']:2d}  "
        f"valid={'OK' if result['ok'] else result['errors']}"
    )
    for violation in report["violations"]:
        print(f"  {'':24s} violation: {violation['mod_id']} {violation['kind']} - {violation['reason']}")


def fresh(mod_names=None, base=os.path.join(ROOT, "mods"), limits=None):
    host = Host("level_a", limits=limits or Limits())
    for name in mod_names or ():
        mod_id, namespace, manifest = load_mod(os.path.join(base, name))
        host.register(mod_id, namespace, manifest)
    return host


def main():
    banner("1. Same fixed level under different mod configurations")
    show("no mods (baseline)", run_scenario(fresh()))
    show("greedy only", run_scenario(fresh(("greedy",))))
    show("thrifty only", run_scenario(fresh(("thrifty",))))
    show("greedy + thrifty", run_scenario(fresh(("greedy", "thrifty"))))
    show("thrifty + greedy (reversed load order)",
         run_scenario(fresh(("thrifty", "greedy"))))

    banner("2. Toggle one mod mid-level and watch the rule change live")
    host = fresh(("greedy",))
    host.begin_level()
    host.tick()
    host.award_score(100)
    print(f"after first award with greedy ON : mult={host.engine.state.multiplier}, score={host.engine.state.score}")
    host.disable("greedy")
    host.award_score(100)
    print(f"after award with greedy OFF     : mult={host.engine.state.multiplier}, score={host.engine.state.score}")
    host.enable("greedy")
    host.award_score(100)
    print(f"after award with greedy ON again: mult={host.engine.state.multiplier}, score={host.engine.state.score}")
    host.end_level()
    host.close()

    banner("3. Illegal mods: load-time rejection")
    for name in ("illegal_import", "illegal_dunder"):
        try:
            load_mod(os.path.join(FIXTURES, name))
            print(f"{name:20s} UNEXPECTEDLY LOADED")
        except LoadError as exc:
            print(f"{name:20s} rejected: {exc}")

    banner("4. Runtime interruptions retain the mod's exact context")
    runtime_cases = (
        ("illegal_command", Limits(), "undeclared command"),
        ("crash", Limits(), "exception inside hook"),
        ("slow_loop", Limits(hook_time_ms=20), "time budget"),
        ("spawn_flood", Limits(), "object cap"),
        ("spawn_bomb", Limits(max_reentrant_dispatches=4, max_event_depth=6,
                              spawns_per_event=6, spawns_per_mod_per_level=12,
                              spawns_total_per_level=24), "recursion"),
    )
    for name, limits, label in runtime_cases:
        host = fresh((name,), base=FIXTURES, limits=limits)
        try:
            run_scenario(host)
        finally:
            host.close()
        ctx = host.context(name)
        print(f"\n{name} ({label})")
        print(f"  suspended={ctx['suspended']}  dispatches={ctx['dispatches']}  "
              f"spawns={ctx['total_spawns']}")
        if ctx["violation"]:
            v = ctx["violation"]
            print(f"  violation: kind={v['kind']} event={v['event']} depth={v['depth']}")
            print(f"             reason={v['reason']}")
            print(f"             submitted commands={v['commands']}")
        # Other mods and the kernel itself are still usable.
        print(f"  level kept running: event_count={host.engine.state.event_count}, "
              f"score intact={host.engine.state.score}")

    banner("5. Recovery: suspended mod can be readmitted, history stays")
    host = fresh(("crash",), base=FIXTURES)
    run_scenario(host)
    before = host.context("crash")
    host.recover("crash")
    after = host.context("crash")
    print(f"suspended before={before['suspended']} after={after['suspended']}")
    print(f"dispatch counters preserved: {after['dispatches'] == before['dispatches']} "
          f"({after['dispatches']} dispatches)")
    host.close()

    banner("6. Unload releases the generation; same-name reinstall is a new one")
    host = fresh()
    mod_id, namespace, manifest = load_mod(os.path.join(ROOT, "mods", "greedy"))
    host.register(mod_id, namespace, manifest)
    probe = capture_generation_probe(host, mod_id)
    host.begin_level()
    host.award_score(100)
    old_api = host.apis[mod_id]
    gen_before = host.context(mod_id)["generation"]
    host.unload(mod_id)
    del namespace
    gc.collect()
    reclaim = probe.report()
    print(f"unloaded generation {gen_before}: references reclaimed={reclaim['reclaimed']}")
    print(f"structural invariants: {structural_reclamation(host)['ok']}")
    try:
        old_api.submit({"cmd": "add_score", "amount": 1})
    except StaleGeneration as exc:
        print(f"old API rejected after unload: {exc.kind}")
    mod_id, namespace2, manifest2 = load_mod(
        os.path.join(ROOT, "mods", "greedy"),
        overrides={"version": "2.0.0", "entry": "mod_v2.py"},
    )
    host.register(mod_id, namespace2, manifest2)
    print(f"reinstalled as generation {host.context(mod_id)['generation']} "
          f"version {host.context(mod_id)['version']}")
    host.award_score(100)
    print(f"level effect now comes from v2: multiplier={host.engine.state.multiplier}")
    host.close()


if __name__ == "__main__":
    main()
