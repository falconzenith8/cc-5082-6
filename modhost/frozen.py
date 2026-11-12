"""Deeply frozen read-only snapshots handed to mod scripts.

Scripts never receive kernel objects. Every state object they can see is
converted into scalar values, tuples or FrozenView mappings; mutation
attempts raise immediately.
"""


class FrozenError(TypeError):
    pass


class FrozenView:
    """Read-only mapping view over a frozen snapshot."""

    def __init__(self, data):
        object.__setattr__(self, "_data", data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def get(self, key, default=None):
        return self._data.get(key, default)

    def to_dict(self):
        """Plain (still immutable at leaf level) dict for diagnostics."""
        return dict(self._data)

    def __setitem__(self, key, value):
        raise FrozenError("state snapshots are read-only")

    def __setattr__(self, key, value):
        raise FrozenError("state snapshots are read-only")

    def __repr__(self):
        return f"FrozenView({self._data!r})"

    def __eq__(self, other):
        if isinstance(other, FrozenView):
            return self._data == other._data
        return NotImplemented

    def __hash__(self):
        return hash(tuple(sorted(self._data.items(), key=lambda kv: kv[0])))


_PRIMITIVES = (str, int, float, bool, type(None))


def freeze(value):
    if isinstance(value, _PRIMITIVES):
        return value
    if isinstance(value, FrozenView):
        return value
    if isinstance(value, dict):
        return FrozenView({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(freeze(v) for v in value))
    raise FrozenError(f"value of type {type(value).__name__} cannot cross the mod boundary")
