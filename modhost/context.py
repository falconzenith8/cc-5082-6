"""Per-mod context and the narrow API object handed to mod code.

Every ModApi carries a generation/epoch token. When a mod is unloaded, or
when the host switches scenes, the old token is revoked: callbacks that
completed late and still hold the old API cannot submit commands into the
new running instance.
"""

from dataclasses import dataclass, field

from .errors import InvalidCommand, StaleGeneration
from .frozen import freeze


@dataclass
class ModToken:
    """Identity of one mod generation inside one host runtime epoch."""

    mod_id: str
    generation: int
    epoch: int

    def matches(self, ctx):
        return (
            self.mod_id == ctx.mod_id
            and self.generation == ctx.generation
            and self.epoch == ctx.epoch
            and not ctx.retired
        )


@dataclass
class ModContext:
    mod_id: str
    name: str
    version: str
    path: str
    reads: tuple
    declares_commands: tuple
    subscriptions: tuple
    generation: int = 1
    epoch: int = 1
    storage: dict = field(default_factory=dict)
    enabled: bool = True
    suspended: bool = False
    retired: bool = False
    retire_reason: str = ""
    # Provenance kept after any interruption.
    violation: object = None
    violation_event: str = ""
    violation_depth: int = 0
    violation_commands: tuple = ()
    violation_snapshot: object = None
    # Counters for limits and diagnostics.
    dispatches: int = 0
    reentrant_chain: int = 0
    spawns_this_level: int = 0
    total_spawns: int = 0
    hook_millis: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    history: list = field(default_factory=list)

    def snapshot(self):
        # Never hand live callables or storage containers across the
        # boundary: expose names and frozen copies only.
        handler_events = tuple(sorted(self.storage.get("handlers", {}).keys()))
        vars_copy = dict(self.storage.get("vars", {}))
        return {
            "mod_id": self.mod_id,
            "name": self.name,
            "version": self.version,
            "generation": self.generation,
            "epoch": self.epoch,
            "enabled": self.enabled,
            "suspended": self.suspended,
            "retired": self.retired,
            "retire_reason": self.retire_reason,
            "handler_events": handler_events,
            "violation": None if self.violation is None else {
                "kind": self.violation.kind,
                "reason": self.violation.reason,
                "event": self.violation_event,
                "depth": self.violation_depth,
                "commands": list(self.violation_commands),
            },
            "dispatches": self.dispatches,
            "spawns_this_level": self.spawns_this_level,
            "total_spawns": self.total_spawns,
            "warnings": list(self.warnings),
            "history": list(self.history),
            "storage": {"vars": freeze(vars_copy)},
            "violation_snapshot": self.violation_snapshot,
        }


class ModApi:
    """The only host object a script can reference. No scene access.

    The API is generation scoped. A retired generation's API still exists
    as a plain object (a late callback may hold it), but every mutating
    call raises StaleGeneration instead of touching the new instance.
    """

    def __init__(self, context, limits, token):
        object.__setattr__(self, "_ctx", context)
        object.__setattr__(self, "_limits", limits)
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_pending", [])
        object.__setattr__(self, "_deferred", [])
        object.__setattr__(self, "_event", None)

    # Called by the host between dispatches; scripts never see this.
    def _bind(self, event):
        self._require_live()
        self._event = event
        self._pending.clear()

    def _drain(self):
        commands = list(self._pending)
        self._pending.clear()
        return commands

    def _drain_deferred(self):
        deferred = list(self._deferred)
        self._deferred.clear()
        return deferred

    def _revoke(self):
        # Unload/scene switch: release every reference the API itself held
        # so the generation can be reclaimed once late callbacks drop it.
        object.__setattr__(self, "_pending", [])
        object.__setattr__(self, "_deferred", [])
        object.__setattr__(self, "_event", None)

    def _require_live(self):
        ctx = self._ctx
        if ctx.retired or not self._token.matches(ctx):
            raise StaleGeneration(
                ctx.mod_id, self._token.generation, ctx.generation,
                self._token.epoch, ctx.epoch,
            )

    def on(self, event, handler):
        """Resubscribe: one slot per event per mod. A second registration
        replaces the first and is recorded as a warning, never stacking."""
        self._require_live()
        existing = self._ctx.storage.setdefault("handlers", {})
        if event in existing:
            self._ctx.warnings.append(
                f"resubscribe to {event!r} replaced previous handler"
            )
        existing[event] = handler

    def submit(self, command):
        self._require_live()
        if self._event is None:
            # A deferred callback cannot smuggle commands in between
            # dispatches: every command must belong to a live hook.
            raise InvalidCommand(
                self._ctx.mod_id, "unbound_submit",
                "submit() is only allowed while a hook is executing",
            )
        if not isinstance(command, dict):
            raise InvalidCommand(
                self._ctx.mod_id, "bad_command",
                f"submit expects a command dict, got {type(command).__name__}",
            )
        if len(self._pending) >= self._limits.commands_per_hook:
            raise InvalidCommand(
                self._ctx.mod_id, "command_budget",
                f"more than {self._limits.commands_per_hook} commands in one hook",
            )
        self._pending.append(dict(command))

    def defer(self, callback, delay_ticks=1):
        """Schedule a script callback that completes after delay_ticks.

        The host owns the queue and runs it on its own ticks. The queued
        entry carries this generation's token; if the mod is unloaded or
        the scene switches before it fires, the entry is discarded and the
        callback is never invoked on the new instance.
        """
        self._require_live()
        if not callable(callback):
            raise InvalidCommand(
                self._ctx.mod_id, "bad_callback",
                f"defer() expects a callable, got {type(callback).__name__}",
            )
        if not isinstance(delay_ticks, int) or isinstance(delay_ticks, bool) or delay_ticks < 1:
            raise InvalidCommand(
                self._ctx.mod_id, "bad_delay",
                "defer() delay_ticks must be a positive int",
            )
        if len(self._deferred) >= self._limits.deferred_per_mod:
            raise InvalidCommand(
                self._ctx.mod_id, "deferred_budget",
                f"more than {self._limits.deferred_per_mod} deferred callbacks queued",
            )
        self._deferred.append((callback, int(delay_ticks)))

    def log(self, message):
        self._require_live()
        self._ctx.history.append(f"{self._event}: {message}")

    # Read-only storage helpers for mod-private scratch state.
    def get_store(self, key, default=None):
        self._require_live()
        return self._ctx.storage.get("vars", {}).get(key, default)

    def set_store(self, key, value):
        self._require_live()
        # Only frozen-safe values may be retained across hooks.
        self._ctx.storage.setdefault("vars", {})[key] = freeze(value)
