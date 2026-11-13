# Mod Host — generation-scoped lifecycle for gameplay mods

A deterministic in-game rules host. Mods run sandboxed hooks
(`on_score`, `on_tick`, `on_death`, `on_spawn`, `on_level_start`), submit
declared commands, and never touch kernel state directly.

## Graphical entry (primary)

The mod-host panel is the in-match operation interface. It is a thin view:
every control posts an action to a local HTTP server that drives the real
`Host` kernel, and the page only renders host-computed state. Failures are
recorded in the host ledger and fed back to the panel — the UI never marks
a mod closed on its own.

```bash
python3 -I panel_launcher.py --port 8765
# open http://127.0.0.1:8765/
```

In the panel you can, per the lifecycle cases:

- load a catalog mod case (including the same-name `greedy_v2` rebuild),
- trigger `on_score` / `on_tick` / `on_death` hooks and read the level
  effects and the last settlement computed by the kernel,
- enable / disable / unload a mod and watch its generation and epoch,
- switch the host scene (new runtime epoch),
- replay a full lifecycle case on a fresh minimal level and inspect the
  reclamation result and stale-drop provenance.

The browser cannot request arbitrary mod paths; only the kernel-approved
catalog in `modhost/panel.py` is loadable.

## Command line (auxiliary only)

The CLI assists the host; it does not implement lifecycle semantics of its
own.

```bash
python3 -I demo.py                      # rule-combination demonstration
python3 -I tools/lifecycle_cli.py       # replay sequences/lifecycle.json
python3 -I tools/reclaim_check.py       # repeated enable/unload reclamation
python3 -I -m unittest discover -s tests
```

## Lifecycle model

- **Generation** — each successful `register()` assigns a monotonic
  generation to one `ModContext` + `ModApi` pair. Re-registering a mod id
  requires unloading first; duplicate registration is rejected.
- **Epoch** — `switch_scene()` starts a new host epoch. Mods keep their
  subscriptions, but every API handle and queued deferred callback from the
  previous epoch is revoked.
- **Disable** — a reversible pause. The participant list is re-checked at
  every hook boundary, so a disable issued while an event is in flight takes
  effect immediately instead of running against a precomputed snapshot.
- **Unload** — full release: handler subscriptions, pending commands,
  deferred callbacks and the module namespace are dropped. Only a plain
  tombstone (no callables) remains for diagnostics. The game itself is not
  restarted and other mods' queued work is untouched.
- **Stale actions** — an unloaded or old-epoch API raises
  `StaleGeneration` on every mutating call. Commands that somehow reach
  settlement with a non-current owner are filtered in `_combine`/`_settle`
  and recorded in `host.stale_drops`; they can never enter the new running
  instance.
- **Deferred callbacks** — `api.defer(fn, delay_ticks)` queues a
  kernel-driven callback carrying the current generation/epoch token. It is
  invoked on a later tick only while that generation is still live; unload
  and scene switches discard it without invoking mod code.
- **Atomic registration** — an entry script that fails validation or raises
  leaves no partial context, API or namespace slot behind.

## Observed defects and the patch

Before the patch, four residual paths were reproduced on the minimal level
(evidence captured in the test suite, not inferred):

1. A control-plane `disable()` issued between participant hooks still let
   the precomputed participant list run the disabled mod's scoring hook.
2. Re-registering a mod id had no generation identity, so pending commands
   from the previous registration could settle under the new one.
3. `disable()` retained handler closures and the whole module namespace;
   context snapshots exposed live callables.
4. A failing `register()` left a context and API registered anyway.

The patch lives in `modhost/context.py`, `modhost/host.py`,
`modhost/sandbox.py`, `modhost/loader.py` and `modhost/limits.py`. The
replayable sequences are in `sequences/lifecycle.json`; reclamation is
verified by weak-reference witnesses plus structural host invariants in
`modhost/lifecycle.py` and `tools/reclaim_check.py`.

## Layout

```
modhost/        host kernel, sandbox, lifecycle runner, graphical panel
mods/           real mods (greedy, thrifty; greedy/mod_v2.py is the rebuild)
tests/          unit tests, HTTP panel tests, hostile fixtures
sequences/      replayable lifecycle sequences (JSON)
tools/          auxiliary CLI runners
panel_launcher.py  graphical entry launcher
```
