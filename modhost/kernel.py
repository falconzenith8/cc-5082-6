"""Tiny deterministic level engine.

This is the real rule simulation the mods influence. State is mutable
inside the kernel but is only ever exposed to mods as frozen snapshots.
Commands are applied to a clone; the live state is replaced only after a
whole combined batch validates, so a failed mod never leaves half a
settlement.
"""

from dataclasses import dataclass, replace


@dataclass
class LevelState:
    level_id: str
    tick: int = 0
    score: int = 0
    multiplier: float = 1.0
    lives: int = 1
    gems: int = 25
    resource_cost: int = 5
    spawned_count: int = 0
    deaths: int = 0
    score_events: int = 0
    event_count: int = 0
    last_event: str = ""
    revive_allowed: bool = False
    objects: tuple = ()
    log: tuple = ()


class Engine:
    def __init__(self, level_id):
        self.state = LevelState(level_id=level_id)

    # --- snapshots -------------------------------------------------------

    def snapshot(self, allowed_keys):
        full = {
            "level_id": self.state.level_id,
            "tick": self.state.tick,
            "score": self.state.score,
            "multiplier": self.state.multiplier,
            "lives": self.state.lives,
            "gems": self.state.gems,
            "resource_cost": self.state.resource_cost,
            "spawned_count": self.state.spawned_count,
            "deaths": self.state.deaths,
            "score_events": self.state.score_events,
            "event_count": self.state.event_count,
            "last_event": self.state.last_event,
        }
        return {key: full[key] for key in allowed_keys if key in full}

    def clone(self):
        return replace(self.state, objects=list(self.state.objects), log=list(self.state.log))

    def commit(self, working):
        working.objects = tuple(working.objects)
        working.log = tuple(working.log)
        self.state = working

    # --- kernel-driven effects (not callable by mods) --------------------

    def award(self, working, base_points):
        gained = int(base_points * working.multiplier)
        working.score += gained
        working.score_events += 1
        working.log.append(f"award {base_points} x{working.multiplier} = {gained}")
        return gained

    def kill(self, working):
        working.lives -= 1
        working.deaths += 1
        working.log.append("death")

    def pay_resource(self, working):
        amount = min(working.resource_cost, working.gems)
        working.gems -= amount
        if amount:
            working.log.append(f"pay {amount} gems")

    # --- command application on a working copy ---------------------------

    def apply(self, working, command):
        name = command["cmd"]
        if name == "set_multiplier":
            working.multiplier = float(command["value"])
        elif name == "add_score":
            working.score += int(command["amount"])
        elif name == "set_revive":
            working.revive_allowed = bool(command["allowed"])
        elif name == "consume_gems":
            amount = int(command["amount"])
            if amount > working.gems:
                raise ValueError(f"cannot consume {amount} gems, only {working.gems} available")
            working.gems -= amount
        elif name == "set_resource_cost":
            working.resource_cost = int(command["cost"])
        elif name == "spawn":
            count = int(command["count"])
            kind = command["kind"]
            working.objects.extend((kind, working.tick) for _ in range(count))
            working.spawned_count += count
        else:  # pragma: no cover - structural validation runs first
            raise ValueError(f"unknown command {name}")
