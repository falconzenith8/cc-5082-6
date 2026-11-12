# Loads fine, then violates its own manifest contract: set_resource_cost
# is structurally valid on on_tick but was never declared in the manifest.
def register(api):
    def on_score(api, state, event):
        api.submit({"cmd": "add_score", "amount": 10})

    def on_tick(api, state, event):
        api.submit({"cmd": "set_resource_cost", "cost": 1})

    api.on("on_score", on_score)
    api.on("on_tick", on_tick)
