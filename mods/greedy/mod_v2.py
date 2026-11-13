# Greedy Risk v2: same mod id, new build. Multiplier is 4.0 instead of the
# v1 rules so a same-name reinstall is observable from level effects alone.
def register(api):
    def on_score(api, state, event):
        api.submit({"cmd": "set_multiplier", "value": 4.0})
        api.log("v2 multiplier 4.0")

    api.on("on_score", on_score)
