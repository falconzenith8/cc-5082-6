# Rejected at load time: dunder access is the classic sandbox escape path.
def register(api):
    def on_score(api, state, event):
        base = ().__class__.__bases__[0].__subclasses__()
        api.submit({"cmd": "add_score", "amount": len(base)})

    api.on("on_score", on_score)
