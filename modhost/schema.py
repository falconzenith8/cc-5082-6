"""Declared readable state keys and submittable command contract.

Mods must declare, in their manifest, which state keys they read and which
commands they may submit. A mod that reaches for an undeclared command is
violating its own contract and is suspended.
"""

from .errors import InvalidCommand


# State keys a mod may ask to see. The kernel only exposes declared keys.
READABLE_KEYS = frozenset({
    "level_id", "tick", "score", "multiplier", "lives", "gems",
    "resource_cost", "spawned_count", "last_event", "event_count",
    "score_events", "deaths",
})

# Per-event command allowlist. Commands submitted outside their event are
# rejected even if the mod declared them.
EVENT_COMMANDS = {
    "on_score": frozenset({"set_multiplier", "add_score"}),
    "on_death": frozenset({"set_revive", "consume_gems", "set_multiplier"}),
    "on_tick": frozenset({"set_resource_cost", "consume_gems", "spawn"}),
    "on_spawn": frozenset({"spawn", "add_score"}),
    "on_level_start": frozenset({"set_multiplier", "set_resource_cost", "spawn"}),
}

ALL_EVENTS = frozenset(EVENT_COMMANDS)
ALL_COMMANDS = frozenset({cmd for cmds in EVENT_COMMANDS.values() for cmd in cmds})

# Structural spec: type and numeric bounds per command field.
_COMMAND_SPECS = {
    "set_multiplier": {"value": (float, int, 1.0, 10.0)},
    "add_score": {"amount": (int, 1, 10000)},
    "set_revive": {"allowed": (bool,)},
    "consume_gems": {"amount": (int, 1, 999)},
    "set_resource_cost": {"cost": (int, 1, 100)},
    "spawn": {"kind": (str,), "count": (int, 1, 8)},
}

_STRING_LIMIT = 32


def validate_structure(mod_id, event, command):
    """Check one command dict against the static command spec.

    Raises InvalidCommand on any structural problem. Semantic checks
    (resources, caps) happen later against a working state copy.
    """
    if not isinstance(command, dict):
        raise InvalidCommand(mod_id, "bad_command", f"command must be a dict, got {type(command).__name__}")
    name = command.get("cmd")
    if not isinstance(name, str):
        raise InvalidCommand(mod_id, "bad_command", "missing string 'cmd' field")
    if name not in ALL_COMMANDS:
        raise InvalidCommand(mod_id, "unknown_command", f"{name!r} is not a known command")
    if name not in EVENT_COMMANDS[event]:
        raise InvalidCommand(mod_id, "event_mismatch", f"command {name!r} not allowed during {event!r}")
    spec = _COMMAND_SPECS[name]
    fields = set(command) - {"cmd"}
    unknown = fields - set(spec)
    if unknown:
        raise InvalidCommand(mod_id, "bad_field", f"command {name!r} has unknown fields {sorted(unknown)}")
    missing = set(spec) - fields
    if missing:
        raise InvalidCommand(mod_id, "bad_field", f"command {name!r} missing fields {sorted(missing)}")
    for field, rule in spec.items():
        value = command[field]
        expected = rule[0]
        # bool is a subclass of int; keep them distinct.
        if expected is int and isinstance(value, bool):
            raise InvalidCommand(mod_id, "bad_field", f"{name}.{field} must be int, not bool")
        if not isinstance(value, expected):
            raise InvalidCommand(mod_id, "bad_field", f"{name}.{field} must be {expected.__name__}")
        if isinstance(value, str) and len(value) > _STRING_LIMIT:
            raise InvalidCommand(mod_id, "bad_field", f"{name}.{field} string too long")
        if len(rule) == 4 and isinstance(value, (int, float)) and not isinstance(value, bool):
            _, _, low, high = rule
            if not low <= value <= high:
                raise InvalidCommand(mod_id, "out_of_range", f"{name}.{field}={value} outside [{low}, {high}]")
