import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modhost import Host, load_mod, run_scenario, validate
from modhost.errors import LoadError
from modhost.frozen import FrozenView, freeze, FrozenError
from modhost.limits import Limits

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODS = os.path.join(ROOT, "mods")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def load_into(host, name, base=MODS):
    mod_id, namespace, manifest = load_mod(os.path.join(base, name))
    host.register(mod_id, namespace, manifest)
    return mod_id


def fresh_host(limits=None):
    return Host("level_a", limits=limits or Limits())


class BaselineAndRealRulesTests(unittest.TestCase):
    def test_baseline_has_no_mod_effects(self):
        report = run_scenario(fresh_host())
        self.assertEqual(report["multiplier"], 1.0)
        self.assertEqual(report["resource_cost"], 5)
        self.assertEqual(report["spawned_count"], 0)
        self.assertEqual(report["violations"], [])
        self.assertGreaterEqual(report["gems"], 0)

    def test_greedy_changes_multiplier_revive_and_spawns(self):
        host = fresh_host()
        load_into(host, "greedy")
        report = run_scenario(host)
        self.assertEqual(report["multiplier"], 3.0)
        self.assertEqual(report["spawned_count"], 1)
        self.assertTrue(any(obj[0] == "coin" for obj in report["objects"]))
        # Paid revive: death happened but a life was restored.
        self.assertEqual(report["lives"], 1)
        self.assertEqual(report["deaths"], 1)
        self.assertEqual(report["violations"], [])
        self.assertGreater(report["score"], 350)

    def test_thrifty_changes_cost_and_multiplier_differently(self):
        host = fresh_host()
        load_into(host, "thrifty")
        report = run_scenario(host)
        self.assertEqual(report["multiplier"], 1.5)
        self.assertEqual(report["resource_cost"], 2)
        self.assertEqual(report["spawned_count"], 0)
        # Cheaper drains leave gems on the table; greedy leaves none.
        self.assertGreater(report["gems"], 0)

    def test_toggle_mid_level_changes_rules_immediately(self):
        host = fresh_host()
        mid = load_into(host, "greedy")
        host.begin_level()
        host.tick()
        # Before any scoring event multiplier is still baseline.
        self.assertEqual(host.engine.state.multiplier, 1.0)
        host.award_score(100)
        self.assertEqual(host.engine.state.multiplier, 2.0)
        host.disable(mid)
        host.award_score(50)
        # Disabled mod cannot keep pushing its multiplier.
        self.assertEqual(host.engine.state.multiplier, 2.0)
        before = host.engine.state.score
        host.award_score(50)
        gained = host.engine.state.score - before
        self.assertEqual(gained, 100)  # 50 * stale 2.0; no new hook ran
        host.enable(mid)
        before = host.engine.state.score
        host.award_score(50)
        gained = host.engine.state.score - before
        self.assertEqual(gained, 150)  # hook active again, streak now pushes 3.0
        self.assertEqual(host.engine.state.multiplier, 3.0)
        host.end_level()

    def test_level_validation_passes_for_clean_runs(self):
        host = fresh_host()
        load_into(host, "greedy")
        load_into(host, "thrifty")
        report = run_scenario(host)
        result = validate(report)
        self.assertTrue(result["ok"], result["errors"])


class CombinationPolicyTests(unittest.TestCase):
    def test_order_conflict_resolves_deterministically(self):
        outcomes = []
        for order in (("greedy", "thrifty"), ("thrifty", "greedy")):
            host = fresh_host()
            for name in order:
                load_into(host, name)
            outcomes.append(run_scenario(host))
        # max-multiplier and min-cost policies are order independent.
        self.assertEqual(outcomes[0]["score"], outcomes[1]["score"])
        self.assertEqual(outcomes[0]["gems"], outcomes[1]["gems"])
        self.assertEqual(outcomes[0]["multiplier"], 3.0)   # max(3.0, 1.5)
        self.assertEqual(outcomes[0]["resource_cost"], 2)  # min policy
        self.assertEqual(outcomes[0]["lives"], outcomes[1]["lives"])

    def test_revive_conflict_is_logical_or(self):
        # Both mods offer a revive; OR means one revive happens and only
        # greedy actually charges gems for it.
        host = fresh_host()
        load_into(host, "greedy")
        load_into(host, "thrifty")
        report = run_scenario(host)
        self.assertEqual(report["lives"], 1)
        greedy_ctx = report["mods"]["greedy"]
        self.assertTrue(any("revive" in line for line in greedy_ctx["history"]))

    def test_add_score_is_summed(self):
        host = fresh_host()
        load_into(host, "thrifty")  # adds +5 per scoring event
        report = run_scenario(host)
        # Three scoring waves, three flat bonuses merged into settlements.
        self.assertEqual(report["score_events"], 3)
        self.assertGreaterEqual(report["score"], 350 + 15)


class ContractAndSandboxTests(unittest.TestCase):
    def test_import_is_rejected_at_load(self):
        with self.assertRaises(LoadError) as ctx:
            load_mod(os.path.join(FIXTURES, "illegal_import"))
        self.assertIn("import", str(ctx.exception))

    def test_dunder_escape_is_rejected_at_load(self):
        with self.assertRaises(LoadError) as ctx:
            load_mod(os.path.join(FIXTURES, "illegal_dunder"))
        self.assertIn("forbidden attribute", str(ctx.exception))

    def test_undeclared_command_suspends_mod_without_touching_state(self):
        host = fresh_host()
        mid = load_into(host, "illegal_command", base=FIXTURES)
        run_scenario(host)
        ctx = host.context(mid)
        self.assertTrue(ctx["suspended"])
        self.assertEqual(ctx["violation"]["kind"], "contract")
        self.assertIn("set_resource_cost", ctx["violation"]["commands"])
        # The settlement containing the undeclared command was fully
        # rejected, so the cost never changed and gems never went negative.
        self.assertEqual(host.engine.state.resource_cost, 5)
        self.assertGreaterEqual(host.engine.state.gems, 0)

    def test_snapshots_are_read_only(self):
        view = freeze({"a": 1, "nested": {"b": [1, 2]}})
        self.assertIsInstance(view, FrozenView)
        with self.assertRaises(FrozenError):
            view["a"] = 2
        with self.assertRaises(FrozenError):
            view.new = 3
        self.assertIsInstance(view["nested"], FrozenView)
        self.assertEqual(view["nested"]["b"], (1, 2))

    def test_mods_only_see_declared_readable_keys(self):
        seen = {}

        host = fresh_host()
        # thrifty declares gems/resource_cost/etc but NOT score_events.
        from modhost.sandbox import compile_mod_source, safe_globals
        source = (
            "def register(api):\n"
            "    def on_score(api, state, event):\n"
            "        api.set_store('seen_gems', state.get('gems', -1))\n"
            "        api.set_store('seen_hidden', state.get('score_events', 'hidden'))\n"
            "    api.on('on_score', on_score)\n"
        )
        code = compile_mod_source(source, "probe")
        ns = safe_globals()
        exec(code, ns)
        manifest = {
            "id": "probe", "name": "probe", "version": "1", "path": "/tmp/probe",
            "entry": "mod.py",
            "reads": ["gems"],
            "commands": [],
            "subscriptions": ["on_score"],
        }
        host.register("probe", ns, manifest)
        host.award_score(10)
        store = host.context("probe")["storage"]["vars"]
        self.assertEqual(store["seen_gems"], host.engine.state.gems)
        self.assertEqual(store["seen_hidden"], "hidden")


class LimitAndFailureTests(unittest.TestCase):
    def test_hook_time_budget_interrupts_and_retains_context(self):
        limits = Limits(hook_time_ms=20)
        host = fresh_host(limits)
        mid = load_into(host, "slow_loop", base=FIXTURES)
        run_scenario(host)
        ctx = host.context(mid)
        self.assertTrue(ctx["suspended"])
        self.assertEqual(ctx["violation"]["kind"], "time_budget")
        self.assertEqual(ctx["violation"]["event"], "on_tick")
        self.assertGreaterEqual(ctx["violation"]["depth"], 1)
        self.assertIsNotNone(ctx["violation_snapshot"])
        self.assertEqual(ctx["violation_snapshot"]["payload"]["event"], "on_tick")
        self.assertIn("state", ctx["violation_snapshot"])
        # Level continued; other events still counted.
        self.assertGreater(host.engine.state.event_count, 1)

    def test_recursion_limit_stops_spawn_bomb(self):
        limits = Limits(max_reentrant_dispatches=4, max_event_depth=6,
                        spawns_per_event=6, spawns_per_mod_per_level=12,
                        spawns_total_per_level=24)
        host = fresh_host(limits)
        mid = load_into(host, "spawn_bomb", base=FIXTURES)
        run_scenario(host)
        ctx = host.context(mid)
        self.assertTrue(ctx["suspended"])
        self.assertIn(ctx["violation"]["kind"], {"recursion", "spawn_limit"})
        # Bounded fan-out, never unbounded object growth.
        self.assertLessEqual(host.engine.state.spawned_count, limits.spawns_total_per_level)
        self.assertLessEqual(max(host.event_log)[0], limits.max_event_depth)

    def test_spawn_cap_rejects_whole_batch(self):
        host = fresh_host()
        mid = load_into(host, "spawn_flood", base=FIXTURES)
        report = run_scenario(host)
        ctx = host.context(mid)
        self.assertTrue(ctx["suspended"])
        self.assertEqual(ctx["violation"]["kind"], "spawn_limit")
        # First bad batch rejected before any flood object appeared.
        self.assertEqual(report["spawned_count"], 0)

    def test_mod_crash_isolates_mod_keeps_level_running(self):
        host = fresh_host()
        mid = load_into(host, "crash", base=FIXTURES)
        report = run_scenario(host)
        ctx = host.context(mid)
        self.assertTrue(ctx["suspended"])
        self.assertEqual(ctx["violation"]["kind"], "crash")
        self.assertIn("boom", ctx["violation"]["reason"])
        # Kernel itself is intact.
        self.assertEqual(report["score"], 350)
        self.assertEqual(report["violations"][0]["mod_id"], mid)

    def test_resubscribe_keeps_single_slot(self):
        host = fresh_host()
        mid = load_into(host, "resubscribe", base=FIXTURES)
        host.begin_level()
        before = host.engine.state.score
        host.award_score(100)
        # Exactly one +7 bonus, not 1000+7+7...
        self.assertEqual(host.engine.state.score - before, 107)
        ctx = host.context(mid)
        self.assertTrue(any("resubscribe" in w for w in ctx["warnings"]))
        host.end_level()

    def test_atomic_settlement_on_cross_mod_failure(self):
        # Greedy's gem consume plus an over-draining fixture combined in
        # one event: either the whole merged settlement applies or none.
        host = fresh_host()
        load_into(host, "greedy")
        # Crash mod contributes nothing valid, but must not roll greedy back.
        load_into(host, "crash", base=FIXTURES)
        report = run_scenario(host)
        self.assertGreaterEqual(report["gems"], 0)
        crashed = host.context("crash")
        self.assertTrue(crashed["suspended"])
        greedy = host.context("greedy")
        self.assertFalse(greedy["suspended"])

    def test_failed_settlement_rolls_back_every_command_in_batch(self):
        # overdraw and thrifty act in the same on_tick. Thrifty's cost
        # change and the gem drain are one merged settlement; when the
        # drain fails, the cost change must roll back too.
        host = fresh_host()
        bad = load_into(host, "overdraw", base=FIXTURES)
        load_into(host, "thrifty")
        gems_at_start = host.engine.state.gems
        host.begin_level()
        host.tick()  # first tick: merged batch fails atomically
        ctx = host.context(bad)
        self.assertTrue(ctx["suspended"])
        self.assertEqual(ctx["violation"]["kind"], "semantic")
        # Thrifty's set_resource_cost was in the rejected batch.
        self.assertEqual(host.engine.state.resource_cost, 5)
        # Only the kernel's own capped payment happened, no mod drain.
        self.assertEqual(host.engine.state.gems, gems_at_start - 5)
        host.end_level()


class LoaderContractTests(unittest.TestCase):
    def test_manifest_unknown_command_rejected(self):
        from modhost.loader import _validate_manifest
        bad = {
            "id": "x", "name": "x", "version": "1", "entry": "m.py",
            "reads": ["score"], "commands": ["nope"], "subscriptions": [],
        }
        with self.assertRaises(LoadError):
            _validate_manifest("x", bad)

    def test_manifest_unknown_read_rejected(self):
        from modhost.loader import _validate_manifest
        bad = {
            "id": "x", "name": "x", "version": "1", "entry": "m.py",
            "reads": ["kernel_pointer"], "commands": [], "subscriptions": [],
        }
        with self.assertRaises(LoadError):
            _validate_manifest("x", bad)

    def test_sandbox_blocks_escape_variants(self):
        from modhost.errors import LoadError as LE
        from modhost.sandbox import compile_mod_source
        attacks = (
            "def register(api):\n    x = ().__class__\n",
            "def register(api):\n    x = globals()\n",
            "def register(api):\n    import json\n",
            "def register(api):\n    x = api.__dict__\n",
            "def register(api):\n    x = eval('1')\n",
            "def register(api):\n    open('/etc/passwd')\n",
        )
        for source in attacks:
            with self.assertRaises(LE, msg=source):
                compile_mod_source(source, "attacker")

    def test_global_spawn_cap_is_enforced_across_mods(self):
        limits = Limits(spawns_per_mod_per_level=12, spawns_total_per_level=3,
                        spawns_per_event=6)
        host = fresh_host(limits)
        # Two well-behaved mods, each small, but together over the global cap.
        from modhost.sandbox import compile_mod_source, safe_globals
        manifest_tpl = {
            "name": "p", "version": "1", "path": "/tmp", "entry": "m.py",
            "reads": ["tick"], "commands": ["spawn"],
            "subscriptions": ["on_level_start"],
        }
        source = (
            "def register(api):\n"
            "    def start(api, state, event):\n"
            "        api.submit({'cmd': 'spawn', 'kind': 'p', 'count': 2})\n"
            "    api.on('on_level_start', start)\n"
        )
        code = compile_mod_source(source, "p")
        for mid in ("pa", "pb"):
            ns = safe_globals()
            exec(code, ns)
            manifest = dict(manifest_tpl, id=mid)
            host.register(mid, ns, manifest)
        host.begin_level()
        # Combined 4 spawns exceed total cap of 3: the whole level-start
        # settlement is rejected and one mod is suspended.
        self.assertLessEqual(host.engine.state.spawned_count, 3)
        self.assertTrue(any(ctx.suspended for ctx in host.mods.values()))
        host.close()

    def test_recover_clears_suspension_but_keeps_history(self):
        host = fresh_host()
        mid = load_into(host, "crash", base=FIXTURES)
        run_scenario(host)
        self.assertTrue(host.context(mid)["suspended"])
        host.recover(mid)
        ctx = host.context(mid)
        self.assertFalse(ctx["suspended"])
        self.assertIsNone(ctx["violation"])
        self.assertGreater(ctx["dispatches"], 0)

    def test_command_from_wrong_event_is_rejected(self):
        host = fresh_host()
        mid = load_into(host, "wrong_event", base=FIXTURES)
        run_scenario(host)
        ctx = host.context(mid)
        self.assertTrue(ctx["suspended"])
        self.assertEqual(ctx["violation"]["kind"], "event_mismatch")
        self.assertEqual(ctx["violation"]["event"], "on_tick")
        self.assertEqual(host.engine.state.revive_allowed, False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
