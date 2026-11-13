"""Auxiliary CLI: verify that unloaded mod generations are fully reclaimed.

Runs repeated enable/unload cycles of the same mod on a minimal level and
checks both the weak-reference witnesses and the host's structural
invariants. The graphical panel is the primary entry point; this command
only assists headless CI verification.

Run:  python3 -I tools/reclaim_check.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modhost import Host
from modhost.lifecycle import (
    capture_generation_probe,
    structural_reclamation,
)
from modhost.loader import load_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CYCLES = 5


def main():
    host = Host("minimal")
    failures = []
    try:
        for cycle in range(1, CYCLES + 1):
            mod_id, namespace, manifest = load_mod(os.path.join(ROOT, "mods", "greedy"))
            host.register(mod_id, namespace, manifest)
            generation = host.mods[mod_id].generation
            probe = capture_generation_probe(host, mod_id)
            host.begin_level()
            host.award_score(100)
            host.tick()
            host.disable(mod_id)
            host.award_score(50)  # disabled generation must not run
            host.unload(mod_id)
            del namespace
            weak = probe.report()
            structural = structural_reclamation(host)
            print(f"cycle {cycle}: generation {generation} "
                  f"reclaimed={weak['reclaimed']} structural_ok={structural['ok']}")
            if not weak["ok"]:
                failures.append(f"cycle {cycle} weakref alive: {weak['alive']}")
            if not structural["ok"]:
                failures.append(f"cycle {cycle} structural: {structural['errors']}")
        report = host.report()
        print(f"\nretired generations: {len(report['retired'])}")
        print(f"stale drops recorded: {len(report['stale_drops'])}")
        print(f"remaining live mods: {sorted(report['mods'])}")
    finally:
        host.close()
    if failures:
        print("\nRECLAMATION FAILED:")
        for failure in failures:
            print(" -", failure)
        return 1
    print(f"\nall {CYCLES} enable/unload cycles reclaimed cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
