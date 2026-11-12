"""Error and violation types for the mod host.

A Violation always identifies the responsible mod and carries a reason.
The host keeps the mod's ModContext after any interruption so the exact
failure is inspectable.
"""


class ModError(Exception):
    """Base class for host-side mod failures."""


class LoadError(ModError):
    """The mod failed static validation and never entered the registry."""


class Violation(ModError):
    """Runtime violation produced by one mod while an event was running."""

    def __init__(self, mod_id, kind, reason):
        super().__init__(f"[{mod_id}] {kind}: {reason}")
        self.mod_id = mod_id
        self.kind = kind
        self.reason = reason


class TimeBudgetExceeded(Violation):
    def __init__(self, mod_id, budget_ms):
        super().__init__(mod_id, "time_budget", f"exceeded {budget_ms} ms hook budget")


class ModTimeoutExceeded(Exception):
    """Raised inside the sandbox by SIGALRM; the host attributes it to a mod."""


class RecursionLimit(Violation):
    def __init__(self, mod_id, limit):
        super().__init__(mod_id, "recursion", f"exceeded reentrant dispatch limit of {limit}")


class SpawnLimit(Violation):
    def __init__(self, mod_id, limit):
        super().__init__(mod_id, "spawn_limit", f"spawned object cap of {limit} reached")


class InvalidCommand(Violation):
    pass


class ModCrashed(Violation):
    def __init__(self, mod_id, exc_type, message):
        super().__init__(mod_id, "crash", f"{exc_type}: {message}")
        self.exc_type = exc_type
