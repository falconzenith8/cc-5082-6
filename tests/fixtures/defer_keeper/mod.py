# Queues a deferred script callback and deliberately keeps a handle on its
# own API. After the mod is unloaded, or the scene switches, a retained
# callback/API belongs to a retired generation and must not touch the new
# running instance.
held = {}


def register(api):
    held["api"] = api

    def on_tick(api, state, event):
        def later(api, state_like):
            # Deferred callbacks keep the command domain of the event that
            # scheduled them: on_tick may adjust resource cost.
            api.submit({"cmd": "set_resource_cost", "cost": 3})

        api.defer(later, 1)
        api.log("deferred callback scheduled")

    def on_spawn(api, state, event):
        api.submit({"cmd": "add_score", "amount": 3})

    def on_score(api, state, event):
        api.submit({"cmd": "add_score", "amount": 1})

    api.on("on_tick", on_tick)
    api.on("on_spawn", on_spawn)
    api.on("on_score", on_score)
