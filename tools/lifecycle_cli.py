"""Auxiliary CLI: replay mod lifecycle sequences and print the kernel trace.

The graphical mod-host panel is the primary entry point
(see README.md); this command only assists headless verification.
Run:  python3 -I tools/lifecycle_cli.py [sequences/lifecycle.json]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modhost.lifecycle import run_all


def main(argv):
    path = argv[1] if len(argv) > 1 else None
    results = run_all(path=path)
    exit_code = 0
    for result in results:
        print(f"[{'OK' if result['ok'] else 'FAIL'}] {result['label']}")
        for entry in result["trace"]:
            step = entry["step"]
            if "error" in entry:
                print(f"    ERROR {step}: {entry['error']}")
            else:
                print(f"    {step} -> {entry['result']}")
        report = result["report"]
        print(f"    score={report['score']} mult={report['multiplier']} "
              f"epoch={report['epoch']} stale_drops={len(report['stale_drops'])} "
              f"retired={len(report['retired'])}")
        if not result["ok"]:
            exit_code = 1
    print(f"\n{sum(r['ok'] for r in results)}/{len(results)} sequences passed")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
