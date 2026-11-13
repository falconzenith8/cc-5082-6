import gc
import os
import sys
import unittest
import weakref

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modhost import (
    Host,
    capture_generation_probe,
    load_mod,
    load_sequences,
    replay_sequence,
    structural_reclamation,
)
from modhost.errors import InvalidCommand, StaleGeneration
from modhost.limits import Limits
from modhost.sandbox import compile_mod_source, safe_globals

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODS = os.path.join(ROOT, "mods")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
SEQ_PATH = os.path.join(ROOT, "sequences", "lifecycle.json")


def make_mod(mod_id, amount, version="1.0.0", tag="v1"):
    src = (
        "held = {}\n"
        "def register(api):\n"
        "    held['api'] = api\n"
        "    def h(api, state, event):\n"
        f"        api.submit({{'cmd': 'add_score', 'amount': {amount}}})\n"
        "        api.log('%s active')\n"
        "    held['handler'] = h\n"
        "    api.on('on_score', h)\n"
    ) % tag
    ns = safe_globals()
    exec(compile_mod_source(src, mod_id), ns)
    manifest = {
        "id": mod_id, "name": mod_id, "version": version, "path": "/tmp",
        "entry": "m.py", "reads": ["score"], "commands": ["add_score"],
        "subscriptions": ["on_score"],
    }
    return ns, manifest


def fresh_host(limits=None):
    return Host("minimal", limits=limits or Limits())


class InFlightLifecycleTests(unittest.TestCase):
    def test_disable_between_hooks_blocks_remaining_participant(self):
        class ControlHost(Host):
            def _run_hook(self, mod_id, event, payload):
                result = super()._run_hook(mod_id, event, payload)
                if event == "on_score" and mod_id == "a":
                    self.disable("b")
                return result

        host = ControlHost("minimal")
        host.register("a", *make_mod("a", 1, tag="a"))
        host.register("b", *make_mod("b", 1000, tag="b"))
        host.begin_level()
        host.award_score(10)
        # Kernel award 10 + a's +1 only; b was disabled before its hook.
        self.assertEqual(host.engine.state.score, 11)
        self.assertFalse(host.mods["b"].enabled)
        host.close()

    def test_unload_between_hooks_keeps_other_mods_legitimate_commands(self):
        class ControlHost(Host):
            def _run_hook(self, mod_id, event, payload):
                result = super()._run_hook(mod_id, event, payload)
                if event == "on_score" and mod_id == "a" and "b" in self.mods:
                    self.unload("b")
                return result

        host = ControlHost("minimal")
        host.register("a", *make_mod("a", 1, tag="a"))
        host.register("b", *make_mod("b", 1000, tag="b"))
        host.begin_level()
        host.award_score(10)
        # b's queued commands are dropped with provenance; a's +1 survives.
        self.assertEqual(host.engine.state.score, 11)
        self.assertNotIn("b", host.mods)
        self.assertTrue(any(d["mod_id"] == "b" for d in host.stale_drops))
        # a keeps participating in later events: its task was not discarded.
        host.award_score(10)
        self.assertEqual(host.engine.state.score, 22)
        host.close()

    def test_unload_during_dispatch_does_not_restart_the_game(self):
        class ControlHost(Host):
            def _run_hook(self, mod_id, event, payload):
                result = super()._run_hook(mod_id, event, payload)
                if event == "on_score" and mod_id == "a":
                    self.unload("b")
                return result

        host = ControlHost("minimal")
        host.register("a", *make_mod("a", 1, tag="a"))
        host.register("b", *make_mod("b", 1000, tag="b"))
        host.begin_level()
        tick_before = host.engine.state.tick
        event_count_before = host.engine.state.event_count
        host.award_score(10)
        # The same engine instance keeps running; no reset.
        self.assertEqual(host.engine.state.tick, tick_before)
        self.assertGreater(host.engine.state.event_count, event_count_before)
        host.close()


class GenerationReinstallTests(unittest.TestCase):
    def test_reinstall_same_name_bumps_generation(self):
        host = fresh_host()
        ns1, man1 = make_mod("swap", 1, version="1.0.0", tag="v1")
        host.register("swap", ns1, man1)
        gen1 = host.mods["swap"].generation
        api1 = host.apis["swap"]
        host.unload("swap")
        self.assertEqual(len(host.retired), 1)

        ns2, man2 = make_mod("swap", 1000, version="2.0.0", tag="v2")
        host.register("swap", ns2, man2)
        gen2 = host.mods["swap"].generation
        self.assertGreater(gen2, gen1)

        host.begin_level()
        host.award_score(10)
        # The v2 build (+1000) runs, not v1 (+1).
        self.assertEqual(host.engine.state.score, 1010)
        # The v1 API handle is now a stale generation.
        with self.assertRaises(StaleGeneration):
            api1.submit({"cmd": "add_score", "amount": 9999})
        with self.assertRaises(StaleGeneration):
            api1.on("on_score", lambda a, s, e: None)
        host.close()

    def test_reload_from_disk_same_id_new_version_changes_level_effect(self):
        host = fresh_host()
        mod_id, ns1, man1 = load_mod(os.path.join(MODS, "greedy"))
        host.register(mod_id, ns1, man1)
        host.begin_level()
        host.award_score(100)
        self.assertEqual(host.engine.state.multiplier, 2.0)
        host.unload("greedy")

        mod_id2, ns2, man2 = load_mod(
            os.path.join(MODS, "greedy"),
            overrides={"version": "2.0.0", "entry": "mod_v2.py"},
        )
        self.assertEqual(mod_id2, "greedy")
        host.register(mod_id2, ns2, man2)
        host.award_score(100)
        self.assertEqual(host.engine.state.multiplier, 4.0)
        self.assertEqual(host.mods["greedy"].version, "2.0.0")
        host.close()

    def test_duplicate_registration_without_unload_is_rejected(self):
        host = fresh_host()
        ns1, man1 = make_mod("dup", 1)
        host.register("dup", ns1, man1)
        ns2, man2 = make_mod("dup", 2)
        with self.assertRaises(InvalidCommand) as ctx:
            host.register("dup", ns2, man2)
        self.assertEqual(ctx.exception.kind, "already_registered")
        # Original generation untouched.
        self.assertEqual(host.mods["dup"].version, "1.0.0")
        host.close()

    def test_stale_api_dropped_command_never_enters_new_instance(self):
        host = fresh_host()
        ns, man = make_mod("swap", 5, tag="v1")
        host.register("swap", ns, man)
        api1 = host.apis["swap"]
        host.begin_level()
        host.award_score(10)
        host.unload("swap")
        ns2, man2 = make_mod("swap", 7, version="2.0.0", tag="v2")
        host.register("swap", ns2, man2)
        with self.assertRaises(StaleGeneration):
            api1.submit({"cmd": "add_score", "amount": 5000})
        host.award_score(10)
        # First award 10+5, second award 10+7 (v2); no 5000 and no v1 effect.
        self.assertEqual(host.engine.state.score, 32)
        host.close()


class ReclamationTests(unittest.TestCase):
    def test_unload_releases_namespace_handlers_and_api(self):
        host = fresh_host()
        ns, man = make_mod("keep", 5, tag="keep")
        host.register("keep", ns, man)
        probe = capture_generation_probe(host, "keep")
        host.begin_level()
        host.award_score(10)
        host.unload("keep")
        del ns
        weak = probe.report()
        self.assertTrue(weak["ok"], weak)
        structural = structural_reclamation(host)
        self.assertTrue(structural["ok"], structural["errors"])
        # Tombstone retains diagnostics but no callables or scratch state.
        tomb = host.retired[-1]
        self.assertTrue(tomb["retired"])
        self.assertEqual(tomb["handler_events"], ())
        host.close()

    def test_disable_keeps_registration_unload_releases_it(self):
        host = fresh_host()
        ns, man = make_mod("soft", 1, tag="soft")
        host.register("soft", ns, man)
        handler_ref = weakref.ref(host.mods["soft"].storage["handlers"]["on_score"])
        host.disable("soft")
        gc.collect()
        # Soft disable is reversible: subscription is intentionally retained.
        self.assertIsNotNone(handler_ref())
        self.assertIn("soft", host.mods)
        host.enable("soft")
        self.assertTrue(host.mods["soft"].enabled)
        host.unload("soft")
        del ns
        gc.collect()
        self.assertIsNone(handler_ref())
        host.close()

    def test_snapshot_never_exposes_live_callables(self):
        host = fresh_host()
        ns, man = make_mod("snap", 1, tag="snap")
        host.register("snap", ns, man)
        snap = host.context("snap")
        self.assertEqual(snap["handler_events"], ("on_score",))
        self.assertNotIn("handlers", snap["storage"])
        # The snapshot is frozen: callers cannot mutate host scratch state.
        from modhost.frozen import FrozenError
        with self.assertRaises(FrozenError):
            snap["storage"]["vars"]["x"] = 123
        host.close()

    def test_failed_registration_leaves_no_residual_slot(self):
        host = fresh_host()
        bad_ns = safe_globals()
        exec(compile_mod_source("def register(api):\n    raise ValueError('nope')\n", "bad"), bad_ns)
        man = {"id": "bad", "name": "bad", "version": "1", "path": "/tmp",
               "entry": "m.py", "reads": [], "commands": [], "subscriptions": []}
        with self.assertRaises(ValueError):
            host.register("bad", bad_ns, man)
        self.assertNotIn("bad", host.mods)
        self.assertNotIn("bad", host.apis)
        self.assertNotIn("bad", host.namespaces)
        host.close()


class SceneSwitchTests(unittest.TestCase):
    def test_scene_switch_invalidates_old_epoch_api(self):
        host = fresh_host()
        ns, man = make_mod("scene", 1, tag="scene")
        host.register("scene", ns, man)
        old_api = host.apis["scene"]
        host.begin_level()
        host.award_score(10)
        host.switch_scene("level_b")
        self.assertEqual(host.scene_id, "level_b")
        self.assertEqual(host.engine.state.level_id, "level_b")
        self.assertEqual(host.engine.state.score, 0)
        with self.assertRaises(StaleGeneration):
            old_api.submit({"cmd": "add_score", "amount": 999})
        # The same mod (same generation) continues under a fresh API.
        self.assertIn("scene", host.mods)
        self.assertEqual(host.mods["scene"].epoch, host.epoch)
        host.award_score(10)
        self.assertEqual(host.engine.state.score, 11)
        host.close()

    def test_scene_switch_drops_pending_deferred_callbacks(self):
        host = fresh_host()
        mod_id, ns, man = load_mod(os.path.join(FIXTURES, "defer_keeper"))
        host.register(mod_id, ns, man)
        host.begin_level()
        host.tick()  # schedules a +7 deferred callback for tick 2
        self.assertEqual(len(host.deferred), 1)
        host.switch_scene("level_b")
        self.assertEqual(host.deferred, [])
        self.assertTrue(any(d["kind"] == "dropped_callback" for d in host.stale_drops))
        host.tick()
        # The callback from the old scene never added its +7.
        self.assertEqual(host.engine.state.score, 0)
        host.close()

    def test_deferred_callback_fires_on_later_tick_with_generation_check(self):
        host = fresh_host()
        mod_id, ns, man = load_mod(os.path.join(FIXTURES, "defer_keeper"))
        host.register(mod_id, ns, man)
        host.begin_level()
        host.tick()  # schedules a resource-cost callback for tick 2
        self.assertEqual(host.engine.state.score, 0)
        host.tick()  # tick 2: deferred callback lowers the cost to 3
        self.assertEqual(host.engine.state.resource_cost, 3)
        # Unloading clears any further queued callbacks.
        host.unload(mod_id)
        self.assertTrue(structural_reclamation(host)["ok"])
        host.close()


    def test_deferred_budget_is_enforced(self):
        host = Host("minimal", limits=Limits(deferred_per_mod=2))
        mod_id, ns, man = load_mod(os.path.join(FIXTURES, "defer_keeper"))
        host.register(mod_id, ns, man)
        api = host.apis[mod_id]
        api._bind("on_tick")

        def cb(api, view):
            return None

        api.defer(cb, 1)
        api.defer(cb, 2)
        with self.assertRaises(InvalidCommand) as ctx:
            api.defer(cb, 3)
        self.assertEqual(ctx.exception.kind, "deferred_budget")
        host.close()

    def test_unload_prunes_only_its_own_deferred_callbacks(self):
        from modhost.host import DeferredCall

        host = fresh_host()
        for name in ("greedy", "thrifty"):
            mod_id, ns, man = load_mod(os.path.join(MODS, name))
            host.register(mod_id, ns, man)
        g = host.mods["greedy"]
        t = host.mods["thrifty"]
        host.deferred.append(DeferredCall(
            "greedy", g.generation, host.epoch, 99, "on_tick", lambda a, v: None))
        host.deferred.append(DeferredCall(
            "thrifty", t.generation, host.epoch, 99, "on_tick", lambda a, v: None))
        host.unload("greedy")
        self.assertEqual([(c.mod_id, c.generation) for c in host.deferred],
                         [("thrifty", t.generation)])
        self.assertIn("thrifty", host.mods)
        self.assertTrue(host.mods["thrifty"].enabled)
        host.close()


class NormalRulesUnchangedTests(unittest.TestCase):
    def test_golden_combined_scenario_matches_original_policy(self):
        from modhost import run_scenario, validate
        from modhost.loader import load_all

        host = Host("level_a")
        load_all(host, MODS)
        report = run_scenario(host)
        self.assertTrue(validate(report)["ok"], validate(report)["errors"])
        self.assertEqual(report["multiplier"], 3.0)
        self.assertEqual(report["resource_cost"], 2)
        self.assertEqual(report["spawned_count"], 1)
        self.assertEqual(report["gems"], 8)
        self.assertEqual(report["score"], 965)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["stale_drops"], [])

    def test_replayable_sequences_all_pass_and_reclaim(self):
        cases = load_sequences(SEQ_PATH)
        self.assertEqual(len(cases), 5)
        for case in cases:
            host = Host("minimal")
            try:
                trace = replay_sequence(host, case["steps"])
            finally:
                host.close()
            for entry in trace:
                self.assertNotIn("error", entry, entry)
        # The sequences that unload must end structurally clean.
        host = Host("minimal")
        try:
            replay_sequence(host, cases[1]["steps"])
            self.assertTrue(structural_reclamation(host)["ok"])
            self.assertNotIn("greedy", host.mods)
        finally:
            host.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
