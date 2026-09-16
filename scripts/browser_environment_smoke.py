"""Offline browser/environment smoke: temporary profiles, no signup or OAuth.

python -m scripts.browser_environment_smoke --engine chromix --browser-executable /path/to/chrome
Use --headed to exercise the same Xvfb/headed mode as production.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.config import BASE_DIR, load_settings
from app.integrations.openai.browser.environment import context_options, environment_summary

PROBE = """() => {
  const canvas = document.createElement('canvas');
  canvas.width = 240; canvas.height = 60;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#e6dfc4'; ctx.fillRect(0, 0, 240, 60);
  ctx.fillStyle = '#345678'; ctx.font = '18px sans-serif';
  ctx.fillText('Team48 browser environment', 5, 30);
  const gl = document.createElement('canvas').getContext('webgl');
  const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
  return {ua: navigator.userAgent, platform: navigator.platform,
    language: navigator.language, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    width: innerWidth, height: innerHeight, screen: [screen.width, screen.height],
    renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
    canvas: canvas.toDataURL()};
}"""


def run(engine: str, executable: str, *, headed: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    settings = load_settings().model_copy(update={
        "browser_engine": engine, "browser_executable": executable,
        "browser_channel": "", "browser_headless": not headed,
        "browser_locale": "en-US", "browser_timezone": "America/Los_Angeles",
    })
    if headed:
        from app.integrations.openai.browser import onboard
        onboard.ensure_virtual_display()
    # An unreachable loopback proxy prevents these test browsers reaching websites.
    proxy = {"server": "http://127.0.0.1:9", "bypass": "<-loopback>"}
    observations = []
    with TemporaryDirectory(prefix="browser-smoke-", dir=BASE_DIR / "data") as temporary:
        with sync_playwright() as pw:
            for name in ("first", "first", "second"):
                options = context_options(Path(temporary) / name, proxy, settings=settings)
                options["args"] += ["--disable-background-networking"]
                context = pw.chromium.launch_persistent_context(**options, timeout=20000)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_content('<button onclick="this.textContent=\'clicked\'">Test</button>')
                    page.get_by_role("button").click(timeout=5000)
                    assert page.get_by_role("button").inner_text() == "clicked"
                    observed = page.evaluate(PROBE)
                    observed["canvas"] = hashlib.sha256(observed["canvas"].encode()).hexdigest()
                    expected = options["viewport"]
                    assert (observed["width"], observed["height"]) == (expected["width"], expected["height"])
                    assert observed["language"] == options["locale"]
                    assert observed["timezone"] == options["timezone_id"]
                    observations.append({"environment": environment_summary(options),
                                         "browser_version": context.browser.version,
                                         "observed": observed})
                finally:
                    context.close()
    assert observations[0] == observations[1], "Environment changed after restarting the same profile"
    if engine == "chromix":
        assert observations[0]["environment"] != observations[2]["environment"], "Independent profiles share configuration"
    return {"ok": True, "same_profile_stable": True, "profiles": observations,
            "note": "Offline DOM and environment checks only; no claim about phone verification"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("chromium", "chromix"), default="chromium")
    parser.add_argument("--browser-executable", default="")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.engine, args.browser_executable, headed=args.headed), ensure_ascii=False))


if __name__ == "__main__":
    main()
