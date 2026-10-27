#!/usr/bin/env python3
"""Status Dashboard launcher app.

Small-screen panel for the Status Dashboard web service
(http://<kvm-ip>:8123, deployed by install.sh as nanokvm-status.service).
Tap anywhere to toggle the service.
"""

import os
import time
import urllib.request

from appbase import GREEN, GRAY, RED, app, rgb565

BG = rgb565(10, 12, 16)
ORANGE = rgb565(255, 120, 90)
ACCENT = rgb565(80, 170, 255)


def svc_active():
    return os.system("systemctl is-active --quiet nanokvm-status 2>/dev/null") == 0


def svc_action(start):
    os.system("systemctl {} nanokvm-status 2>/dev/null".format("start" if start else "stop"))


def draw(ctx, state):
    fb = ctx.fb
    fb.clear(BG)
    w = ctx.width
    fb.text_center(w // 2, 6, "DASHBOARD", ACCENT, 2)

    if state == "on":
        fb.text_center(w // 2, 26, "RUNNING :8123", GREEN, 1)
        fb.text_center(w // 2, 38, "web + /api/status", GRAY, 1)
        fb.text_center(w // 2, 52, "TAP TO STOP", ORANGE, 1)
    elif state == "off":
        fb.text_center(w // 2, 26, "STOPPED", RED, 1)
        fb.text_center(w // 2, 38, "service: nanokvm-status", GRAY, 1)
        fb.text_center(w // 2, 52, "TAP TO START", GREEN, 1)
    else:  # switching
        fb.text_center(w // 2, 30, "SWITCHING...", GRAY, 1)
    fb.flush()


@ app()
def main(ctx):
    state = "on" if svc_active() else "off"
    draw(ctx, state)
    while True:
        tapped = False
        for _kind, _x, _y in ctx.taps():
            tapped = True
        if tapped:
            draw(ctx, "switching")
            svc_action(not svc_active())
            time.sleep(1.0)
            state = "on" if svc_active() else "off"
            draw(ctx, state)
        time.sleep(0.2)
