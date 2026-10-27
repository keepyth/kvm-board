#!/usr/bin/env python3
"""Status Dashboard launcher app.

Small-screen panel for the Status Dashboard web service
(http://<kvm-ip>:8123, deployed by post-install script as
nanokvm-status.service). Tap anywhere to toggle the service.
"""

import os
import sys
import urllib.request

sys.path.insert(0, "/kvmcomm/apps")  # SDK 目录（appbase 所在）

from appbase import GREEN, GRAY, RED, WHITE, AppContext, app, rgb565

BG = rgb565(10, 12, 16)
ORANGE = rgb565(255, 120, 90)
ACCENT = rgb565(80, 170, 255)


def svc_active():
    return os.system("systemctl is-active --quiet nanokvm-status 2>/dev/null") == 0


def svc_action(start):
    os.system("systemctl {} nanokvm-status 2>/dev/null".format("start" if start else "stop"))


def web_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:8123/api/status", timeout=2)
        return True
    except Exception:
        return False


@app()
def main(ctx: AppContext) -> None:
    state = {"on": svc_active(), "switching": False}

    def on_tap(x: int, y: int) -> None:
        if state["switching"]:
            return
        state["switching"] = True
        svc_action(not state["on"])
        state["on"] = svc_active()
        state["switching"] = False

    def tick(dt: float) -> None:
        ctx.fb.clear(BG)
        w = ctx.width
        ctx.fb.text_center(w // 2, 6, "DASHBOARD", ACCENT, 2)
        if state["on"]:
            ok = web_up()
            ctx.fb.text_center(w // 2, 26, "RUNNING :8123", GREEN if ok else ORANGE, 1)
            ctx.fb.text_center(w // 2, 38, "web + /api/status" + ("" if ok else " (api down)"), GRAY, 1)
            ctx.fb.text_center(w // 2, 52, "TAP TO STOP", ORANGE, 1)
        else:
            ctx.fb.text_center(w // 2, 26, "STOPPED", RED, 1)
            ctx.fb.text_center(w // 2, 38, "service: nanokvm-status", GRAY, 1)
            ctx.fb.text_center(w // 2, 52, "TAP TO START", GREEN, 1)

    ctx.run(tick, fps=2, on_tap=on_tap)


if __name__ == "__main__":
    main()
