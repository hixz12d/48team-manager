"""Offline browser/environment smoke: temporary profiles, no signup or OAuth.

python -m scripts.browser_environment_smoke --engine camoufox
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
from app.integrations.openai.browser.environment import launch_browser_context

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
    intl_locale: Intl.DateTimeFormat().resolvedOptions().locale,
    cores: navigator.hardwareConcurrency, webdriver: navigator.webdriver === true,
    width: innerWidth, height: innerHeight, outer: [outerWidth, outerHeight],
    screen: [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth],
    dpr: devicePixelRatio,
    renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
    canvas: canvas.toDataURL()};
}"""
TIMEZONE = "America/Los_Angeles"


def run(engine: str, executable: str, *, headed: bool = False) -> dict:
    settings = load_settings().model_copy(update={
        "browser_engine": engine, "browser_executable": executable,
        "browser_channel": "", "browser_headless": not headed,
        "browser_locale": "en-US", "browser_timezone": TIMEZONE,
    })
    # An unreachable loopback proxy prevents these test browsers reaching websites.
    bypass = "localhost,127.0.0.1,::1" if engine == "camoufox" else "<-loopback>"
    proxy = {"server": "http://127.0.0.1:9", "bypass": bypass}
    observations = []
    with TemporaryDirectory(prefix="browser-smoke-", dir=BASE_DIR / "data", ignore_cleanup_errors=True) as temporary:
        for name in ("first", "first", "second"):
            with launch_browser_context(Path(temporary) / name, proxy, settings=settings) as (context, page, _, summary):
                page.set_content('<button onclick="this.textContent=\'clicked\'">Test</button>')
                page.get_by_role("button").click(timeout=10000)
                assert page.get_by_role("button").inner_text() == "clicked"
                observed = page.evaluate(PROBE)
                # Camoufox covers canvas readback with random data (another OS than the host).
                observed["canvas"] = (None if engine == "camoufox"
                                      else hashlib.sha256(observed["canvas"].encode()).hexdigest())
                if engine == "chromium":
                    assert (observed["width"], observed["height"]) == (1280, 900)
                assert observed["language"] == settings.browser_locale, observed["language"]
                assert observed["timezone"] == TIMEZONE, observed["timezone"]
                assert not observed["webdriver"], "navigator.webdriver is exposed"
                browser = getattr(context, "browser", None)
                observations.append({"environment": summary,
                                     "browser_version": browser.version if browser else "",
                                     "observed": observed})
    first, again = observations[0], observations[1]
    changed = sorted({k for k in first["observed"] if first["observed"][k] != again["observed"].get(k)})
    assert first == again, f"Environment changed after restarting the same profile: {changed or first['environment']}"
    if engine == "chromix":
        assert observations[0]["environment"] != observations[2]["environment"], "Independent profiles share configuration"
    if engine == "camoufox":
        assert observations[0]["observed"] != observations[2]["observed"], "Independent profiles share a fingerprint"
    return {"ok": True, "same_profile_stable": True, "profiles": observations,
            "note": "Offline DOM and environment checks only; no claim about phone verification"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("chromium", "chromix", "camoufox"), default="chromium")
    parser.add_argument("--browser-executable", default="")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.engine, args.browser_executable, headed=args.headed), ensure_ascii=False))


if __name__ == "__main__":
    main()
