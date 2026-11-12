"""Mod loader: manifest contract, sandbox compilation, isolated execution.

A mod directory contains:
  manifest.json - id/name/version/entry plus declared reads, commands and
                  event subscriptions. The manifest is the capability list.
  *.py          - entry script, executed once in a sandboxed namespace.

The loader never runs code from the mod directory as a module and never
puts that directory on sys.path; the source text is read and compiled
through the AST gate instead.
"""

import json
import os
import re

from .errors import LoadError
from .sandbox import compile_mod_source, safe_globals
from .schema import ALL_COMMANDS, ALL_EVENTS, READABLE_KEYS

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_REQUIRED = ("id", "name", "version", "entry", "reads", "commands", "subscriptions")


def _validate_manifest(mod_id, manifest):
    missing = [key for key in _REQUIRED if key not in manifest]
    if missing:
        raise LoadError(f"[{mod_id}] manifest missing fields: {missing}")
    if not _ID_RE.match(str(manifest["id"])):
        raise LoadError(f"[{mod_id}] manifest id {manifest['id']!r} is not a valid slug")
    if manifest["id"] != mod_id:
        raise LoadError(f"[{mod_id}] manifest id {manifest['id']!r} does not match directory")
    for key in ("name", "version", "entry"):
        if not isinstance(manifest[key], str) or not manifest[key]:
            raise LoadError(f"[{mod_id}] manifest field {key!r} must be a non-empty string")
    for key in ("reads", "commands", "subscriptions"):
        if not isinstance(manifest[key], list) or not all(isinstance(v, str) for v in manifest[key]):
            raise LoadError(f"[{mod_id}] manifest field {key!r} must be a list of strings")
    unknown_reads = set(manifest["reads"]) - READABLE_KEYS
    if unknown_reads:
        raise LoadError(f"[{mod_id}] undeclared readable keys: {sorted(unknown_reads)}")
    unknown_commands = set(manifest["commands"]) - ALL_COMMANDS
    if unknown_commands:
        raise LoadError(f"[{mod_id}] unknown commands: {sorted(unknown_commands)}")
    unknown_events = set(manifest["subscriptions"]) - ALL_EVENTS
    if unknown_events:
        raise LoadError(f"[{mod_id}] unknown events: {sorted(unknown_events)}")
    # Entry path must stay inside the mod directory.
    entry = manifest["entry"]
    if os.path.isabs(entry) or ".." in entry.split(os.sep) or not entry.endswith(".py"):
        raise LoadError(f"[{mod_id}] entry path {entry!r} is not a safe relative .py path")


def load_mod(path):
    """Load one mod directory. Returns (mod_id, namespace, manifest)."""
    path = os.path.abspath(path)
    mod_id = os.path.basename(path)
    if not _ID_RE.match(mod_id):
        raise LoadError(f"directory name {mod_id!r} is not a valid mod id")
    manifest_path = os.path.join(path, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise LoadError(f"[{mod_id}] manifest.json not found")
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except json.JSONDecodeError as exc:
        raise LoadError(f"[{mod_id}] manifest is not valid JSON: {exc}") from exc
    _validate_manifest(mod_id, manifest)

    entry_path = os.path.abspath(os.path.join(path, manifest["entry"]))
    if os.path.dirname(entry_path) != path:
        raise LoadError(f"[{mod_id}] entry escapes mod directory")
    with open(entry_path, "r", encoding="utf-8") as handle:
        source = handle.read()
    code = compile_mod_source(source, mod_id)
    namespace = safe_globals()
    try:
        exec(code, namespace)  # noqa: S102 - gated AST plus restricted globals
    except LoadError:
        raise
    except Exception as exc:
        raise LoadError(f"[{mod_id}] entry raised {type(exc).__name__}: {exc}") from exc
    if not callable(namespace.get("register")):
        raise LoadError(f"[{mod_id}] entry defines no register(api) function")
    manifest["path"] = path
    return mod_id, namespace, manifest


def load_all(host, directory, only=None):
    """Load every immediate subdirectory of `directory` into the host."""
    loaded = []
    for name in sorted(os.listdir(directory)):
        full = os.path.join(directory, name)
        if not os.path.isdir(full) or name.startswith((".", "_")):
            continue
        if only is not None and name not in only:
            continue
        mod_id, namespace, manifest = load_mod(full)
        host.register(mod_id, namespace, manifest)
        loaded.append(mod_id)
    return loaded
