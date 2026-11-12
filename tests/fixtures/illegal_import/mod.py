# Rejected at load time: imports are outside the sandbox contract.
import os


def register(api):
    def on_score(api, state, event):
        api.submit({"cmd": "add_score", "amount": len(os.listdir("."))})

    api.on("on_score", on_score)
