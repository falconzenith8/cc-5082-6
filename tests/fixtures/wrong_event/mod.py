# set_revive is declared and valid in itself, but it only belongs to
# on_death. Submitting it during on_tick breaks the event allowlist.
def register(api):
    def on_tick(api, state, event):
        api.submit({"cmd": "set_revive", "allowed": True})

    def on_death(api, state, event):
        api.submit({"cmd": "set_revive", "allowed": True})

    api.on("on_tick", on_tick)
    api.on("on_death", on_death)
