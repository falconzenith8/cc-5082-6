# Greedy Risk: high score multiplier, paid revives, bonus gems for streaks.
# Every effect goes through api.submit; the mod never touches kernel state.


def register(api):
    def on_score(api, state, event):
        streak = state.get("score_events", 0)
        if streak >= 1:
            api.submit({"cmd": "set_multiplier", "value": 3.0})
            api.log("pushed multiplier to 3.0")
        else:
            api.submit({"cmd": "set_multiplier", "value": 2.0})

    def on_death(api, state, event):
        gems = state.get("gems", 0)
        lives = state.get("lives", 1)
        if lives <= 0 and gems >= 4:
            api.submit({"cmd": "set_revive", "allowed": True})
            api.submit({"cmd": "consume_gems", "amount": 4})
            api.log("bought a revive for 4 gems")

    def on_tick(api, state, event):
        # One streak coin mid-level, paid for with a skimmed gem.
        tick = state.get("tick", 0)
        if tick == 3 and state.get("gems", 0) > 1:
            api.submit({"cmd": "spawn", "kind": "coin", "count": 1})
            api.submit({"cmd": "consume_gems", "amount": 1})
            api.log("streak coin spawned, 1 gem skimmed")

    api.on("on_score", on_score)
    api.on("on_death", on_death)
    api.on("on_tick", on_tick)
