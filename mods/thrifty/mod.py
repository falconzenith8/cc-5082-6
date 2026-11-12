# Thrifty Rules: cheaper resource drains, modest multiplier, free revive
# once per level. Conflicts with Greedy on purpose so the combination
# policy is observable.


def register(api):
    def on_tick(api, state, event):
        # Halve the baseline cost, floor at 2.
        cost = state.get("resource_cost", 5)
        target = max(2, cost // 2)
        if target != cost:
            api.submit({"cmd": "set_resource_cost", "cost": target})

    def on_score(api, state, event):
        api.submit({"cmd": "set_multiplier", "value": 1.5})
        # Small flat thrift bonus on every scoring event.
        api.submit({"cmd": "add_score", "amount": 5})

    def on_death(api, state, event):
        if state.get("lives", 1) <= 0 and not api.get_store("revive_used", False):
            api.submit({"cmd": "set_revive", "allowed": True})
            api.set_store("revive_used", True)
            api.log("free revive granted once")

    api.on("on_tick", on_tick)
    api.on("on_score", on_score)
    api.on("on_death", on_death)
