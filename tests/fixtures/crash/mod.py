# Raises inside a hook. The host must treat it as a mod crash, suspend
# only this mod, and keep the level (and other mods) running.
def register(api):
    def on_score(api, state, event):
        raise ValueError("boom from mod code")

    api.on("on_score", on_score)
