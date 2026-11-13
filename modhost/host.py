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

Lifecycle: each registration is a mod *generation* inside a host *epoch*.
Unloading a mod revokes its generation; switching scenes starts a new
epoch. Commands and deferred callbacks carrying a stale token are dropped
before they can touch the running instance, and unloaded subscriptions
release their handler/namespace references.
"""

import time

from .context import ModApi, ModContext, ModToken
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


class DeferredCall:
    """A script callback that completes on a later host tick."""

    __slots__ = ("mod_id", "generation", "epoch", "due_tick", "event", "callback")

    def __init__(self, mod_id, generation, epoch, due_tick, event, callback):
        self.mod_id = mod_id
        self.generation = generation
        self.epoch = epoch
        self.due_tick = due_tick
        self.event = event
        self.callback = callback


class Host:
    def __init__(self, level_id, limits=LIMITS):
        self.engine = Engine(level_id)
        self.scene_id = level_id
        self.limits = limits
        self.mods = {}          # mod_id -> ModContext
        self.apis = {}          # mod_id -> ModApi
        self.namespaces = {}    # mod_id -> mod module namespace (released on unload)
        self.timer = AlarmTimer()
        self.timer.install()
        self.depth = 0
        self.event_spawns = 0
        self.event_log = []
        self.violations = []
        # Monotonic identity: every registration gets a new generation,
        # every scene switch a new epoch.
        self._generation_counter = 0
        self.epoch = 1
        self.deferred = []      # DeferredCall queue, kernel-driven
        self.stale_drops = []   # provenance for every rejected stale action
        self.retired = []       # plain-dict tombstones of unloaded generations
        self.last_settlement = None

    def close(self):
        self.timer.shutdown()

    # --- generation / identity ------------------------------------------

    def _next_generation(self):
        self._generation_counter += 1
        return self._generation_counter

    def _record_stale(self, mod_id, generation, epoch, kind, reason):
        entry = {
            "mod_id": mod_id,
            "generation": generation,
            "epoch": epoch,
            "current_generation": self.mods[mod_id].generation if mod_id in self.mods else None,
            "current_epoch": self.epoch,
            "kind": kind,
            "reason": reason,
            "tick": self.engine.state.tick,
        }
        self.stale_drops.append(entry)
        return entry

    def _is_live(self, ctx):
        return (
            not ctx.retired
            and ctx.epoch == self.epoch
            and self.mods.get(ctx.mod_id) is ctx
        )

    def _is_active(self, ctx):
        return (
            self._is_live(ctx)
            and ctx.enabled
            and not ctx.suspended
        )

    # --- mod lifecycle ---------------------------------------------------

    def register(self, mod_id, namespace, manifest):
        if mod_id in self.mods:
            raise InvalidCommand(
                mod_id, "already_registered",
                f"mod {mod_id!r} generation {self.mods[mod_id].generation} is still loaded; "
                "unload it before registering another version",
            )
        generation = self._next_generation()
        ctx = ModContext(
            mod_id=mod_id,
            name=manifest["name"],
            version=manifest["version"],
            path=manifest["path"],
            reads=tuple(manifest["reads"]),
            declares_commands=tuple(manifest["commands"]),
            subscriptions=tuple(manifest["subscriptions"]),
            generation=generation,
            epoch=self.epoch,
        )
        api = ModApi(ctx, self.limits, ModToken(mod_id, generation, self.epoch))
        register_fn = namespace.get("register")
        if not callable(register_fn):
            raise InvalidCommand(mod_id, "bad_module", "entry script has no register(api) function")
        # Keep the namespace unreachable from the rest of the host until the
        # entry script has proven valid; failure leaves nothing registered.
        try:
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
        except Exception:
            # Atomic registration: a rejected entry never occupies a slot
            # and its partial subscriptions are discarded with the object.
            api._revoke()
            ctx.storage.clear()
            raise
        self.mods[mod_id] = ctx
        self.apis[mod_id] = api
        self.namespaces[mod_id] = namespace
        return ctx

    def enable(self, mod_id):
        ctx = self._require(mod_id)
        if ctx.suspended:
            return False
        ctx.enabled = True
        return True

    def disable(self, mod_id):
        ctx = self._require(mod_id)
        ctx.enabled = False

    def unload(self, mod_id, reason="unload"):
        """Release one mod generation: subscriptions, pending commands,
        deferred callbacks and every reference to its script namespace."""
        if mod_id not in self.mods:
            raise KeyError(f"unknown mod {mod_id!r}")
        ctx = self.mods[mod_id]
        api = self.apis[mod_id]
        generation, epoch = ctx.generation, ctx.epoch
        # Drop this generation's deferred callbacks before they can fire.
        self.deferred = [
            call for call in self.deferred
            if not (call.mod_id == mod_id and call.generation == generation and call.epoch == epoch)
        ]
        api._revoke()
        self._retire_context(ctx, reason)
        del self.mods[mod_id]
        del self.apis[mod_id]
        # Removing the namespace entry breaks the host-side strong
        # reference to the module globals (handler closures may still be
        # on a running stack; they die as soon as that hook returns).
        self.namespaces.pop(mod_id, None)
        self._record_stale(mod_id, generation, epoch, "unload", reason)
        return self.retired[-1]

    def _retire_context(self, ctx, reason):
        ctx.retired = True
        ctx.enabled = False
        ctx.retire_reason = reason
        # Release handler closures, scratch state and snapshots first; the
        # tombstone is then built from the cleared context, so it carries
        # diagnostics only and never exposes callables.
        ctx.storage.clear()
        ctx.violation_snapshot = None
        tombstone = ctx.snapshot()
        self.retired.append(tombstone)

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
            if self._is_active(self.mods[mid])
            and event in self.mods[mid].storage.get("handlers", {})
        ]

    # --- level / scene lifecycle ----------------------------------------

    def begin_level(self):
        for ctx in self.mods.values():
            ctx.spawns_this_level = 0
            ctx.reentrant_chain = 0
        # A new level run cannot inherit script callbacks queued previously.
        for call in self.deferred:
            self._record_stale(call.mod_id, call.generation, call.epoch,
                               "dropped_callback", "new level run invalidated the previous queue")
        self.deferred = []
        self.raise_event("on_level_start", {})

    def end_level(self):
        return self.report()

    def switch_scene(self, scene_id):
        """Move the running host to a new scene without restarting the
        game. Registered mods keep their subscriptions, but every API and
        deferred callback from the previous epoch is invalidated."""
        self.epoch += 1
        self.scene_id = scene_id
        self.engine = Engine(scene_id)
        self.depth = 0
        self.event_spawns = 0
        self.event_log = []
        # Every callback queued in the old scene is now stale; record it.
        for call in self.deferred:
            self._record_stale(call.mod_id, call.generation, call.epoch,
                               "dropped_callback", "scene switch invalidated the previous epoch")
        self.deferred = []
        # Re-issue every API against the new epoch while keeping the same
        # mod generation and enabled/suspended state.
        for mod_id in list(self.mods):
            ctx = self.mods[mod_id]
            old_api = self.apis[mod_id]
            old_api._revoke()
            ctx.epoch = self.epoch
            ctx.spawns_this_level = 0
            ctx.reentrant_chain = 0
            self.apis[mod_id] = ModApi(
                ctx, self.limits, ModToken(mod_id, ctx.generation, self.epoch),
            )
        self.raise_event("on_level_start", {})

    def report(self):
        s = self.engine.state
        return {
            "level_id": s.level_id,
            "scene_id": self.scene_id,
            "epoch": self.epoch,
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
            "stale_drops": list(self.stale_drops),
            "last_settlement": self.last_settlement,
            "deferred_queued": [
                {
                    "mod_id": c.mod_id, "generation": c.generation,
                    "epoch": c.epoch, "due_tick": c.due_tick, "event": c.event,
                }
                for c in self.deferred
            ],
            "retired": list(self.retired),
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
            # Re-check at the hook boundary: a disable/unload issued while
            # the dispatch is in flight takes effect immediately instead of
            # running against a precomputed snapshot.
            ctx = self.mods.get(mod_id)
            if ctx is None or not self._is_active(ctx):
                continue
            ctx.reentrant_chain += 1
            if ctx.reentrant_chain > self.limits.max_reentrant_dispatches:
                self._suspend(
                    ctx, RecursionLimit(mod_id, self.limits.max_reentrant_dispatches),
                    event, payload, [],
                )
                continue
            commands, violation = self._run_hook(mod_id, event, payload)
            if violation is not None:
                if self._is_live(ctx):
                    self._suspend(ctx, violation, event, payload, commands)
                else:
                    self._record_stale(
                        mod_id, ctx.generation, ctx.epoch, "stale_violation",
                        f"{violation.kind} after generation retired",
                    )
                continue
            # The generation may have been retired between the hook start
            # and its completion (reentrant control action).
            ctx = self.mods.get(mod_id)
            if ctx is None or not self._is_active(ctx):
                self._record_stale(
                    mod_id, ctx.generation if ctx is not None else None,
                    ctx.epoch if ctx is not None else self.epoch,
                    "dropped_commands",
                    "mod disabled or unloaded before settlement",
                )
                continue
            collected[mod_id] = commands

        merged, merge_error = self._combine(event, collected)
        if merge_error is not None:
            bad_mod, violation = merge_error
            if bad_mod in self.mods and self._is_live(self.mods[bad_mod]):
                self._suspend(self.mods[bad_mod], violation, event, payload,
                              collected.get(bad_mod, []))
            return

        self._settle(event, merged)

    def _settle(self, event, merged):
        # Final generation gate: never settle a command whose owner
        # generation is no longer current.
        accepted = []
        for command, owner in merged:
            if owner == "<kernel>":
                accepted.append((command, owner))
                continue
            ctx = self.mods.get(owner)
            if ctx is None or not self._is_live(ctx):
                self._record_stale(
                    owner,
                    ctx.generation if ctx is not None else None,
                    ctx.epoch if ctx is not None else self.epoch,
                    "dropped_commands",
                    "stale generation rejected at settlement",
                )
                continue
            accepted.append((command, owner))
        if not accepted:
            self.last_settlement = {"event": event, "applied": [], "spawned": 0}
            return

        working = self.engine.clone()
        try:
            for command, owner in accepted:
                self.engine.apply(working, command)
        except ValueError as exc:
            # Whole settlement is rejected; state stays untouched.
            self._suspend(
                self.mods[owner],
                InvalidCommand(owner, "semantic", str(exc)),
                event, freeze({"event": event}), [command],
            )
            return

        pre_spawn_total = self.engine.state.spawned_count
        self.engine.commit(working)

        # Spawn accounting only happens for generations that actually
        # survived into the committed settlement.
        for _, owner in accepted:
            ctx = self.mods.get(owner)
            if ctx is None:
                continue
            spawn_count = sum(
                int(cmd["count"]) for cmd, cmd_owner in accepted
                if cmd_owner == owner and cmd.get("cmd") == "spawn"
            )
            if spawn_count:
                ctx.spawns_this_level += spawn_count
                ctx.total_spawns += spawn_count

        new_spawns = self.engine.state.spawned_count - pre_spawn_total
        self.last_settlement = {
            "event": event,
            "applied": [dict(cmd, owner=owner) for cmd, owner in accepted],
            "spawned": new_spawns,
        }

        # Reentrant spawn events, driven only by newly spawned objects.
        # Depth and per-mod dispatch caps bound the fan-out.
        if new_spawns > 0:
            owners = sorted(
                mid for mid in self.mods
                if any(cmd.get("cmd") == "spawn" for cmd, owner in accepted if owner == mid)
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
            if mod_id in self.mods and self.mods[mod_id] is ctx:
                ctx.dispatches += 1
                ctx.hook_millis.append((time.perf_counter() - start) * 1000.0)
        commands = api._drain()
        deferred = api._drain_deferred()

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
        # Only successful hooks may leave callbacks for later ticks; a
        # suspended generation must not schedule work it never gets to run.
        self._enqueue_deferred(mod_id, ctx, deferred, event)
        return commands, None

    def _enqueue_deferred(self, mod_id, ctx, deferred, event):
        for callback, delay_ticks in deferred:
            self.deferred.append(DeferredCall(
                mod_id=mod_id,
                generation=ctx.generation,
                epoch=ctx.epoch,
                due_tick=self.engine.state.tick + delay_ticks,
                event=event,
                callback=callback,
            ))

    def _run_due_deferred(self):
        """Kernel-driven: invoke deferred script callbacks whose tick has
        arrived. Stale entries are discarded without invoking code."""
        if not self.deferred:
            return
        current_tick = self.engine.state.tick
        due = [call for call in self.deferred if call.due_tick <= current_tick]
        self.deferred = [call for call in self.deferred if call.due_tick > current_tick]
        # Deterministic order: due tick, then mod id.
        due.sort(key=lambda c: (c.due_tick, c.mod_id))
        for call in due:
            ctx = self.mods.get(call.mod_id)
            if ctx is None:
                self._record_stale(call.mod_id, call.generation, call.epoch,
                                   "dropped_callback", "mod unloaded before deferred callback fired")
                continue
            if ctx.retired or ctx.generation != call.generation or ctx.epoch != call.epoch:
                self._record_stale(call.mod_id, call.generation, call.epoch,
                                   "dropped_callback", "deferred callback belongs to a retired generation")
                continue
            if not self._is_active(ctx):
                self._record_stale(call.mod_id, call.generation, call.epoch,
                                   "dropped_callback", "mod disabled or suspended before deferred callback fired")
                continue
            api = self.apis[call.mod_id]
            commands, violation = self._invoke_deferred(ctx, api, call)
            if violation is not None:
                self._suspend(ctx, violation, call.event, freeze({"event": call.event}), commands)
                continue
            if not self._is_active(ctx):
                self._record_stale(ctx.mod_id, ctx.generation, ctx.epoch,
                                   "dropped_commands", "mod retired while a deferred callback ran")
                continue
            self._dispatch_deferred_settlement(ctx, api, call, commands)

    def _invoke_deferred(self, ctx, api, call):
        mod_id = ctx.mod_id
        event = call.event
        api._bind(event)
        start = time.perf_counter()
        self.timer.arm(self.limits.hook_time_ms / 1000.0)
        try:
            call.callback(api, self._view_for(ctx))
        except ModTimeoutExceeded:
            return api._drain(), TimeBudgetExceeded(mod_id, self.limits.hook_time_ms)
        except Violation as exc:
            return api._drain(), exc
        except Exception as exc:
            return api._drain(), ModCrashed(mod_id, type(exc).__name__, str(exc))
        finally:
            self.timer.disarm()
            if mod_id in self.mods and self.mods[mod_id] is ctx:
                ctx.dispatches += 1
                ctx.hook_millis.append((time.perf_counter() - start) * 1000.0)
        commands = api._drain()
        deferred = api._drain_deferred()
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
        self._enqueue_deferred(mod_id, ctx, deferred, event)
        return commands, None

    def _dispatch_deferred_settlement(self, ctx, api, call, commands):
        collected = {ctx.mod_id: commands}
        merged, merge_error = self._combine(call.event, collected)
        if merge_error is not None:
            bad_mod, violation = merge_error
            if bad_mod in self.mods and self._is_live(self.mods[bad_mod]):
                self._suspend(self.mods[bad_mod], violation, call.event,
                              freeze({"event": call.event}), commands)
            return
        self._settle(call.event, merged)

    def _view_for(self, ctx):
        return freeze(self.engine.snapshot(set(ctx.reads)))

    # --- combination -----------------------------------------------------

    def _combine(self, event, collected):
        """Return (merged commands, None) or (None, (mod_id, Violation)).

        Only currently live generations contribute to a settlement; any
        stale owner is filtered before caps and merge rules run, so other
        mods' legitimate commands are never discarded.
        """
        merged = []
        # Deterministic mod order.
        ordered = [
            mid for mid in sorted(collected)
            if mid in self.mods and self._is_live(self.mods[mid])
        ]
        for stale_mid in sorted(set(collected) - set(ordered)):
            self._record_stale(stale_mid, None, self.epoch, "dropped_commands",
                               "mod not live at combine time")

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

        self.event_spawns += total_new
        return merged, None

    # --- suspension / provenance ----------------------------------------

    def _suspend(self, ctx, violation, event, payload, commands):
        if ctx.retired or self.mods.get(ctx.mod_id) is not ctx:
            # Never resurrect or mutate a retired generation.
            self._record_stale(ctx.mod_id, ctx.generation, ctx.epoch,
                               "dropped_violation", f"{violation.kind} after retirement")
            return
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
        # Deferred script callbacks are kernel-driven after each tick's
        # settlement, never run on their own thread.
        self._run_due_deferred()
