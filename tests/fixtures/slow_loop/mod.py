# Burns the hook time budget; the host must interrupt via SIGALRM and
# suspend the mod while the level keeps running.
def register(api):
    def on_tick(api, state, event):
        total = 0
        while True:
            total += 1
        api.submit({"cmd": "add_score", "amount": total})

    api.on("on_tick", on_tick)
