# Asks for more gems than exist. The merged settlement must roll back as
# a unit: gems stay non-negative and no companion command partially lands.
def register(api):
    def on_tick(api, state, event):
        api.submit({"cmd": "consume_gems", "amount": 500})

    api.on("on_tick", on_tick)
