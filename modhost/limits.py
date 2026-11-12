"""Fixed resource limits enforced by the host."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    # Wall-clock budget for one hook invocation of one mod (milliseconds).
    hook_time_ms: int = 50
    # How many events (including re-entrant ones) one mod may participate
    # in while a single outer event is being dispatched.
    max_reentrant_dispatches: int = 16
    # Maximum event nesting depth, regardless of which mod causes it.
    max_event_depth: int = 8
    # Objects one mod may spawn during one level run.
    spawns_per_mod_per_level: int = 12
    # Total objects all mods may spawn during one level run.
    spawns_total_per_level: int = 24
    # Spawned objects that may be emitted while processing one event.
    spawns_per_event: int = 6
    # Commands one mod may submit in a single hook.
    commands_per_hook: int = 16


LIMITS = Limits()
