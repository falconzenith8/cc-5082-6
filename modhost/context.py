"""Per-mod context and the narrow API object handed to mod code."""

from dataclasses import dataclass, field

from .errors import InvalidCommand
from .frozen import freeze


@dataclass
class ModContext:
    mod_id: str
    name: str
    version: str
    path: str
    reads: tuple
    declares_commands: tuple
    subscriptions: tuple
    storage: dict = field(default_factory=dict)
    enabled: bool = True
    suspended: bool = False
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
        return {
            "mod_id": self.mod_id,
            "name": self.name,
            "version": self.version,
            "enabled": self.enabled,
            "suspended": self.suspended,
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
            "storage": dict(self.storage),
            "violation_snapshot": self.violation_snapshot,
        }


class ModApi:
    """The only host object a script can reference. No scene access."""

    def __init__(self, context, limits):
        object.__setattr__(self, "_ctx", context)
        object.__setattr__(self, "_limits", limits)
        object.__setattr__(self, "_pending", [])
        object.__setattr__(self, "_event", None)

    # Called by the host between dispatches; scripts never see this.
    def _bind(self, event):
        self._event = event
        self._pending.clear()

    def _drain(self):
        commands = list(self._pending)
        self._pending.clear()
        return commands

    def on(self, event, handler):
        """Resubscribe: one slot per event per mod. A second registration
        replaces the first and is recorded as a warning, never stacking."""
        existing = self._ctx.storage.setdefault("handlers", {})
        if event in existing:
            self._ctx.warnings.append(
                f"resubscribe to {event!r} replaced previous handler"
            )
        existing[event] = handler

    def submit(self, command):
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

    def log(self, message):
        self._ctx.history.append(f"{self._event}: {message}")

    # Read-only storage helpers for mod-private scratch state.
    def get_store(self, key, default=None):
        return self._ctx.storage.get("vars", {}).get(key, default)

    def set_store(self, key, value):
        # Only frozen-safe values may be retained across hooks.
        self._ctx.storage.setdefault("vars", {})[key] = freeze(value)
