# Each tick asks for more objects than the per-mod level cap allows.
# The whole batch must be rejected before any object appears.
def register(api):
    def on_tick(api, state, event):
        api.submit({"cmd": "spawn", "kind": "flood", "count": 8})

    api.on("on_tick", on_tick)
