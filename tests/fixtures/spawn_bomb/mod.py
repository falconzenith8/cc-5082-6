# Reentrant chain: every spawn event spawns again. The host must cut the
# recursion at its dispatch/depth limits and retain the context.
def register(api):
    def on_tick(api, state, event):
        api.submit({"cmd": "spawn", "kind": "bomb", "count": 1})

    def on_spawn(api, state, event):
        api.submit({"cmd": "spawn", "kind": "bomb", "count": 1})

    api.on("on_tick", on_tick)
    api.on("on_spawn", on_spawn)
