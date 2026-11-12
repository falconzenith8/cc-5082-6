# Registers the same event repeatedly. Policy: one slot per event, later
# handler wins, no stacked callbacks. Only one +7 per scoring event may
# ever land.
def register(api):
    def first(api, state, event):
        api.submit({"cmd": "add_score", "amount": 1000})

    def second(api, state, event):
        api.submit({"cmd": "add_score", "amount": 7})

    api.on("on_score", first)
    api.on("on_score", second)
    api.on("on_score", second)

    def on_score(api, state, event):
        # Runtime resubscribe must also replace, not stack.
        api.on("on_score", second)
        api.submit({"cmd": "add_score", "amount": 7})

    api.on("on_score", on_score)
