"""Mod host: event dispatch, command combination, limits and provenance.

Deterministic combination policy for one event across all enabled mods
(mods run in mod_id order):

* set_multiplier  -> the maximum value wins
* add_score       -> all amounts are summed
* set_revive      -> logical OR of every allowed flag
* set_resource_cost -> the minimum value wins (cheapest rule)
* consume_gems    -> amounts are summed, then checked once atomically
* spawn           -> grouped by kind, counts summed, caps checked atomically

Every command is first structurally validated, then the whole merged batch
is semantically validated and applied to a clone in one step. Any failure
rejects the entire settlement; the live state never moves halfway.
"""

import time

from .context import ModApi, ModContext
from .errors import (
    InvalidCommand,
    ModCrashed,
    ModTimeoutExceeded,
    RecursionLimit,
    SpawnLimit,
    TimeBudgetExceeded,
    Violation,
)
from .frozen import freeze
from .kernel import Engine
from .limits import LIMITS
from .sandbox import AlarmTimer
from .schema import EVENT_COMMANDS, validate_structure


class Host:
    def __init__(self, level_id, limits=LIMITS):
        self.engine = Engine(level_id)
        self.limits = limits
        self.mods = {}  # mod_id -> ModContext
        self.apis = {}  # mod_id -> ModApi
        self.timer = AlarmTimer()
        self.timer.install()
        self.depth = 0
        self.event_spawns = 0
        self.event_log = []
        self.violations = []

    def close(self):
        self.timer.shutdown()

    # --- mod lifecycle ---------------------------------------------------

    def register(self, mod_id, namespace, manifest):
        ctx = ModContext(
            mod_id=mod_id,
            name=manifest["name"],
            version=manifest["version"],
            path=manifest["path"],
            reads=tuple(manifest["reads"]),
            declares_commands=tuple(manifest["commands"]),
            subscriptions=tuple(manifest["subscriptions"]),
        )
        api = ModApi(ctx, self.limits)
        self.mods[mod_id] = ctx
        self.apis[mod_id] = api
        # Mods register handlers by calling api.on inside register().
        register_fn = namespace.get("register")
        if not callable(register_fn):
            raise InvalidCommand(mod_id, "bad_module", "entry script has no register(api) function")
        register_fn(api)
        handlers = ctx.storage.get("handlers", {})
        unknown = set(handlers) - set(EVENT_COMMANDS)
        if unknown:
            raise InvalidCommand(mod_id, "bad_event", f"subscribed to unknown events {sorted(unknown)}")
        undeclared = set(handlers) - set(ctx.subscriptions)
        if undeclared:
            raise InvalidCommand(
                mod_id, "contract",
                f"registered events {sorted(undeclared)} not declared in manifest",
            )
        return ctx

    def enable(self, mod_id):
        ctx = self._require(mod_id)
        if ctx.suspended:
            return False
        ctx.enabled = True
        return True

    def disable(self, mod_id):
        self._require(mod_id).enabled = False

    def recover(self, mod_id):
        """Re-admit a suspended mod into future events. The recorded
        violation context stays available for inspection."""
        ctx = self._require(mod_id)
        if not ctx.suspended:
            return
        ctx.suspended = False
        ctx.violation = None
        ctx.violation_event = ""
        ctx.violation_commands = ()
        ctx.enabled = True

    def context(self, mod_id):
        return self._require(mod_id).snapshot()

    def _require(self, mod_id):
        try:
            return self.mods[mod_id]
        except KeyError:
            raise KeyError(f"unknown mod {mod_id!r}") from None

    def _participating(self, event):
        return [
            mid for mid in sorted(self.mods)
            if self.mods[mid].enabled
            and not self.mods[mid].suspended
            and event in self.mods[mid].storage.get("handlers", {})
        ]

    # --- level lifecycle -------------------------------------------------

    def begin_level(self):
        for ctx in self.mods.values():
            ctx.spawns_this_level = 0
            ctx.reentrant_chain = 0
        self.raise_event("on_level_start", {})

    def end_level(self):
        return self.report()

    def report(self):
        s = self.engine.state
        return {
            "level_id": s.level_id,
            "score": s.score,
            "multiplier": s.multiplier,
            "lives": s.lives,
            "gems": s.gems,
            "resource_cost": s.resource_cost,
            "spawned_count": s.spawned_count,
            "objects": list(s.objects),
            "deaths": s.deaths,
            "score_events": s.score_events,
            "violations": [
                {"mod_id": v.mod_id, "kind": v.kind, "reason": v.reason}
                for v in self.violations
            ],
            "mods": {mid: ctx.snapshot() for mid, ctx in sorted(self.mods.items())},
        }

    # --- event dispatch --------------------------------------------------

    def raise_event(self, event, payload):
        if self.depth >= self.limits.max_event_depth:
            # Attribute the structural depth breach to the mod whose
            # handler emitted the deepest nested event.
            owner = getattr(self, "_depth_owner", None)
            mod_id = owner or "<kernel>"
            violation = RecursionLimit(mod_id, self.limits.max_event_depth)
            if mod_id in self.mods:
                ctx = self.mods[mod_id]
                self._suspend(
                    ctx, violation, event,
                    freeze(dict(payload, event=event)), [],
                )
            else:
                self.violations.append(violation)
            return
        self.depth += 1
        if self.depth == 1:
            # A fresh outer event: every mod's reentrancy chain restarts.
            for ctx in self.mods.values():
                ctx.reentrant_chain = 0
            self.event_spawns = 0
        self.engine.state.event_count += 1
        self.engine.state.last_event = event
        frozen_payload = freeze(dict(payload, event=event))
        self.event_log.append((self.depth, event))
        try:
            self._dispatch(event, frozen_payload)
        finally:
            self.depth -= 1

    def _dispatch(self, event, payload):
        participants = self._participating(event)
        collected = {}  # mod_id -> raw commands
        for mod_id in participants:
            ctx = self.mods[mod_id]
            ctx.reentrant_chain += 1
            if ctx.reentrant_chain > self.limits.max_reentrant_dispatches:
                self._suspend(
                    ctx, RecursionLimit(mod_id, self.limits.max_reentrant_dispatches),
                    event, payload, [],
                )
                continue
            commands, violation = self._run_hook(mod_id, event, payload)
            if violation is not None:
                self._suspend(ctx, violation, event, payload, commands)
                continue
            collected[mod_id] = commands

        merged, merge_error = self._combine(event, collected)
        if merge_error is not None:
            bad_mod, violation = merge_error
            self._suspend(self.mods[bad_mod], violation, event, payload, collected.get(bad_mod, []))
            return

        working = self.engine.clone()
        try:
            for command, owner in merged:
                self.engine.apply(working, command)
        except ValueError as exc:
            # Whole settlement is rejected; state stays untouched.
            self._suspend(
                self.mods[owner],
                InvalidCommand(owner, "semantic", str(exc)),
                event, payload, collected.get(owner, []),
            )
            return

        pre_spawn_total = self.engine.state.spawned_count
        self.engine.commit(working)

        # Reentrant spawn events, driven only by newly spawned objects.
        # Depth and per-mod dispatch caps bound the fan-out.
        new_spawns = self.engine.state.spawned_count - pre_spawn_total
        if new_spawns > 0:
            owners = sorted(
                mid for mid in collected
                if any(cmd.get("cmd") == "spawn" for cmd in collected[mid])
            )
            self._depth_owner = owners[0] if owners else getattr(self, "_depth_owner", None)
            try:
                self._emit_spawn_events(new_spawns)
            finally:
                if event != "on_spawn":
                    self._depth_owner = None

    def _emit_spawn_events(self, new_spawns):
        # One reentrant event per newly spawned object, bounded by the
        # per-event cap; further objects simply do not fan out.
        for _ in range(min(new_spawns, self.limits.spawns_per_event)):
            self.raise_event("on_spawn", {"reason": "spawned"})

    def _run_hook(self, mod_id, event, payload):
        ctx = self.mods[mod_id]
        api = self.apis[mod_id]
        handler = ctx.storage["handlers"][event]
        api._bind(event)
        start = time.perf_counter()
        self.timer.arm(self.limits.hook_time_ms / 1000.0)
        try:
            handler(api, self._view_for(ctx), payload)
        except ModTimeoutExceeded:
            return api._drain(), TimeBudgetExceeded(mod_id, self.limits.hook_time_ms)
        except Violation as exc:
            return api._drain(), exc
        except Exception as exc:  # any mod exception is a mod crash, never a kernel failure
            return api._drain(), ModCrashed(mod_id, type(exc).__name__, str(exc))
        finally:
            self.timer.disarm()
            ctx.dispatches += 1
            ctx.hook_millis.append((time.perf_counter() - start) * 1000.0)
        commands = api._drain()

        # Static validation and manifest contract, per command.
        for command in commands:
            try:
                validate_structure(mod_id, event, command)
            except InvalidCommand as exc:
                return commands, InvalidCommand(mod_id, exc.kind, exc.reason)
            if command["cmd"] not in ctx.declares_commands:
                return commands, InvalidCommand(
                    mod_id, "contract",
                    f"command {command['cmd']!r} not declared in manifest",
                )
        return commands, None

    def _view_for(self, ctx):
        return freeze(self.engine.snapshot(set(ctx.reads)))

    # --- combination -----------------------------------------------------

    def _combine(self, event, collected):
        """Return (merged commands, None) or (None, (mod_id, Violation))."""
        merged = []
        # Deterministic mod order.
        ordered = [mid for mid in sorted(collected)]

        multiplier = None
        mult_owner = None
        revive = False
        cost = None
        cost_owner = None
        score_total = 0
        gems_total = 0
        spawns = {}
        spawn_owner = {}

        for mod_id in ordered:
            for command in collected[mod_id]:
                name = command["cmd"]
                if name == "set_multiplier":
                    value = float(command["value"])
                    if multiplier is None or value > multiplier:
                        multiplier, mult_owner = value, mod_id
                elif name == "add_score":
                    score_total += int(command["amount"])
                elif name == "set_revive":
                    revive = revive or bool(command["allowed"])
                elif name == "set_resource_cost":
                    value = int(command["cost"])
                    if cost is None or value < cost:
                        cost, cost_owner = value, mod_id
                elif name == "consume_gems":
                    gems_total += int(command["amount"])
                elif name == "spawn":
                    kind = command["kind"]
                    spawns[kind] = spawns.get(kind, 0) + int(command["count"])
                    spawn_owner[kind] = mod_id

        # Spawn caps are checked before anything is built.
        total_new = sum(spawns.values())
        if self.event_spawns + total_new > self.limits.spawns_per_event:
            owner = ordered[0] if ordered else "<kernel>"
            return None, (owner, SpawnLimit(owner, self.limits.spawns_per_event))
        for mod_id in ordered:
            mod_spawn = sum(
                int(cmd["count"]) for cmd in collected[mod_id] if cmd["cmd"] == "spawn"
            )
            ctx = self.mods[mod_id]
            if mod_spawn > 0 and (
                ctx.spawns_this_level + mod_spawn > self.limits.spawns_per_mod_per_level
                or self.engine.state.spawned_count + total_new > self.limits.spawns_total_per_level
            ):
                return None, (mod_id, SpawnLimit(mod_id, self.limits.spawns_per_mod_per_level))

        if multiplier is not None:
            merged.append(({"cmd": "set_multiplier", "value": multiplier}, mult_owner))
        if score_total:
            merged.append(({"cmd": "add_score", "amount": score_total}, ordered[0]))
        if event == "on_death":
            merged.append(({"cmd": "set_revive", "allowed": revive}, ordered[0] if ordered else "<kernel>"))
        if cost is not None:
            merged.append(({"cmd": "set_resource_cost", "cost": cost}, cost_owner))
        if gems_total:
            merged.append(({"cmd": "consume_gems", "amount": gems_total}, ordered[0]))
        for kind in sorted(spawns):
            merged.append(({"cmd": "spawn", "kind": kind, "count": spawns[kind]}, spawn_owner[kind]))

        # Bookkeeping: attribute the combined spawn counts back to each mod.
        for mod_id in ordered:
            mod_spawn = sum(
                int(cmd["count"]) for cmd in collected[mod_id] if cmd["cmd"] == "spawn"
            )
            self.mods[mod_id].spawns_this_level += mod_spawn
            self.mods[mod_id].total_spawns += mod_spawn
        self.event_spawns += total_new
        return merged, None

    # --- suspension / provenance ----------------------------------------

    def _suspend(self, ctx, violation, event, payload, commands):
        ctx.suspended = True
        ctx.enabled = False
        ctx.violation = violation
        ctx.violation_event = event
        ctx.violation_depth = self.depth
        ctx.violation_commands = tuple(c.get("cmd") if isinstance(c, dict) else str(c) for c in commands)
        ctx.violation_snapshot = freeze({
            "state": self.engine.snapshot(set(ctx.reads)),
            "payload": payload,
        })
        self.violations.append(violation)

    # --- kernel-level gameplay actions used by the level script ---------

    def award_score(self, base_points):
        # Hooks run first so multiplier changes affect this very award.
        self.raise_event("on_score", {"base": base_points})
        working = self.engine.clone()
        gained = self.engine.award(working, base_points)
        self.engine.commit(working)
        return gained

    def die(self):
        working = self.engine.clone()
        self.engine.kill(working)
        working.revive_allowed = False
        self.engine.commit(working)
        # Revive hooks only matter when this death was fatal.
        if self.engine.state.lives > 0:
            return False
        self.raise_event("on_death", {"lives_left": self.engine.state.lives})
        if self.engine.state.revive_allowed:
            working = self.engine.clone()
            working.lives = 1
            self.engine.commit(working)
            return True
        return False

    def tick(self):
        working = self.engine.clone()
        working.tick += 1
        self.engine.commit(working)
        # Hooks may adjust resource cost or consume gems before payment.
        self.raise_event("on_tick", {"tick": self.engine.state.tick})
        working = self.engine.clone()
        self.engine.pay_resource(working)
        self.engine.commit(working)
