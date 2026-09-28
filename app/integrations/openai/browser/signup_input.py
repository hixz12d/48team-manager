"""Validated input adapter for the isolated signup world (protocol v1).

No page-supplied CDP method is executed. Unknown outcomes stop the run rather
than allowing the content script to release a submission reservation and retry.
"""
from __future__ import annotations

import math

PROTOCOL_VERSION = 1
CAPABILITIES = ("key", "text", "move", "click", "wheel", "date-layout")
SPECIAL_KEYS = {"Backspace", "Tab", "Enter", "Escape", "Home", "End", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown"}


class SignupInput:
    def __init__(self, page, cdp, state):
        self.page, self.cdp, self.state = page, cdp, state

    def handle(self, message, *, current):
        kind = message.get("type")
        if (self.state.status != "running" or message.get("jobId") != self.state.id
                or message.get("version") != self.state.version or not current()):
            return {"active": False, "sent": False}
        if self.state.clock() - self.state.started >= 600:
            self.state.fail("registration_timeout", "注册等待超时，请继续同一邮箱处理")
            return {"active": False, "sent": False}
        sent = False

        def check():
            if self.state.status != "running" or not current():
                raise RuntimeError("signup input context changed")

        def send(method, params):
            nonlocal sent
            check()
            # Even a missing CDP reply may mean the browser dispatched this action.
            sent = True
            return self.cdp.send(method, params)

        try:
            if kind == "input-ready":
                self.cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
                return {"trusted": True, "protocol": PROTOCOL_VERSION, "capabilities": list(CAPABILITIES)}
            if kind == "input-key":
                key = message.get("key")
                if not isinstance(key, str) or not (key in SPECIAL_KEYS or len(key) == 1):
                    raise ValueError("invalid key")
                check()
                sent = True
                # Always release a pressed key, including navigation or cancellation.
                try:
                    self.page.keyboard.down(key)
                finally:
                    self.page.keyboard.up(key)
            elif kind == "input-text":
                text = message.get("text")
                if not isinstance(text, str) or not 0 < len(text) <= 256:
                    raise ValueError("invalid text")
                send("Input.insertText", {"text": text})
            elif kind in {"input-move", "input-click", "input-wheel"}:
                x, y = message.get("x"), message.get("y")
                if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v < 20000 for v in (x, y)):
                    raise ValueError("invalid point")
                if kind == "input-wheel":
                    dx, dy = message.get("deltaX", 0), message.get("deltaY")
                    if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 600 for v in (dx, dy)):
                        raise ValueError("invalid wheel")
                    send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy})
                else:
                    send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                    if kind == "input-click":
                        button = {"x": x, "y": y, "button": "left", "clickCount": 1}
                        try:
                            send("Input.dispatchMouseEvent", {"type": "mousePressed", "buttons": 1, **button})
                        finally:
                            # Release even if cancellation/navigation happened after press.
                            self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "buttons": 0, **button})
            elif kind == "input-date-layout":
                index = message.get("index")
                if type(index) is not int or index < 0:
                    raise ValueError("invalid date index")
                root = self.cdp.send("DOM.getDocument", {"depth": 0})["root"]
                ids = self.cdp.send("DOM.querySelectorAll", {"nodeId": root["nodeId"], "selector": 'input[type="date"]'})["nodeIds"]
                node = self.cdp.send("DOM.describeNode", {"nodeId": ids[index], "depth": -1, "pierce": True})["node"]
                parts = []

                def visit(item):
                    attrs = item.get("attributes", [])
                    for i in range(0, len(attrs), 2):
                        if attrs[i] == "pseudo":
                            for part in ("year", "month", "day"):
                                if attrs[i + 1] == f"-webkit-datetime-edit-{part}-field":
                                    parts.append(part)
                    for child in item.get("children", []) + item.get("shadowRoots", []):
                        visit(child)

                visit(node)
                if len(parts) != 3 or len(set(parts)) != 3:
                    raise ValueError("unrecognized date layout")
                return {"parts": parts}
            elif kind == "input-drift":
                return {}  # Optional idle motion has no effect on registration.
            else:
                raise ValueError("unsupported input")
            return {"sent": True}
        except Exception:
            self.state.fail("registration_input_unknown" if sent else "registration_input_failed",
                            "浏览器输入结果未确认，已停止；请核对后继续同一邮箱")
            return {"active": False, "sent": sent, "uncertain": sent}
