"""Real Chromium layout/hit testing and CDP clicks; no live accounts or network."""
import unittest

from playwright.sync_api import sync_playwright
from tests.browser_signup_extension import EXTENSION, MOCK_RUNTIME


class SignupVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def fixture(self, html):
        context = self.browser.new_context(viewport={"width": 900, "height": 650})
        self.addCleanup(context.close)
        page = context.new_page()
        context.route('**/*', lambda route: route.fulfill(content_type='text/html',
                      body='<style>body{margin:0}button{width:240px;height:60px}</style>' + html))
        page.goto('https://auth.openai.com/create-account')
        page.add_script_tag(content=MOCK_RUNTIME)
        page.fixture_cdp = context.new_cdp_session(page)
        page.evaluate("""() => {
            window.fixtureInputs = [];
            const send = chrome.runtime.sendMessage;
            chrome.runtime.sendMessage = async message => {
                const result = await send(message);
                return message.type.startsWith('input-') ? new Promise(resolve => {
                    fixtureInputs.push(message); window.resolveInput = resolve;
                }) : result;
            };
            document.querySelector('#target').onclick = event => {
                window.clicked = (window.clicked || 0) + 1;
                window.trustedClick = event.isTrusted;
            };
        }""")
        # Expose only the existing interaction boundary; do not mock browser geometry or hit tests.
        source = (EXTENSION / "content.js").read_text(encoding="utf-8")
        self.assertEqual(source.count("  startLoop();"), 1)
        source = source.replace("  startLoop();", """
  trustedInput = true; job = testState;
  window.fixtureClick = stage => clickControl(document.querySelector('#target'), stage);
""")
        page.add_script_tag(content=source)
        return page

    def test_collapsed_submit_container_recovers_without_manual_resume(self):
        page = self.fixture('<div id="clip" style="height:0;overflow:hidden">'
                            '<button id="target">Continue</button></div>')
        page.evaluate("setTimeout(()=>document.querySelector('#clip').style.height='100px', 1800)")
        self.assertEqual(self.click(page, 'email'), {"value": True})
        self.assertEqual(page.evaluate("window.clicked"), 1)
        self.assertTrue(page.evaluate("window.trustedClick"))
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='pause')"))

    def test_hidden_submit_button_waits_for_layout(self):
        page = self.fixture('<button id="target" style="visibility:hidden">Continue</button>')
        page.evaluate("setTimeout(()=>document.querySelector('#target').style.visibility='visible', 1700)")
        self.assertEqual(self.click(page, 'email'), {"value": True})
        self.assertEqual(page.evaluate("window.clicked"), 1)
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='pause')"))

    def test_clipped_button_replacement_is_resolved_during_wait(self):
        page = self.fixture('<div id="clip" style="height:0;overflow:hidden">'
                            '<button id="target">Continue</button></div>')
        page.evaluate("""setTimeout(()=>{
            const old=document.querySelector('#target'), fresh=old.cloneNode(true);
            fresh.onclick=old.onclick; old.replaceWith(fresh);
            document.querySelector('#clip').style.height='100px';
        }, 1700)""")
        self.assertEqual(self.click(page, 'email'), {"value": True})
        self.assertEqual(page.evaluate("window.clicked"), 1)

    def test_permanently_clipped_button_times_out_without_claiming_submit(self):
        page = self.fixture('<div style="height:0;overflow:hidden"><button id="target">Continue</button></div>')
        # Exercise the real 15 s deadline without spending wall time on an idle page.
        page.evaluate("setTimeout(()=>{const now=Date.now;Date.now=()=>now()+16000}, 1500)")
        self.assertEqual(self.click(page, 'email'), {"value": False})
        self.assertEqual(page.evaluate("testState.pauseCode"), 'button_unavailable')
        self.assertFalse(page.evaluate("!!window.clicked || !!testClaims.email"))

    def test_pause_while_waiting_for_clipped_button_never_submits(self):
        page = self.fixture('<div id="clip" style="height:0;overflow:hidden">'
                            '<button id="target">Continue</button></div>')
        page.evaluate("setTimeout(()=>{testState.active=false;document.querySelector('#clip').style.height='100px'}, 700)")
        result = self.click(page, 'email')
        self.assertTrue(result == {'value': False} or result.get('error') == 'StopStep', result)
        self.assertFalse(page.evaluate("!!window.clicked || !!testClaims.email"))

    def click(self, page, stage=None):
        page.evaluate("stage => {fixtureClick(stage).then(value=>window.fixtureResult={value}, error=>window.fixtureResult={error:error.constructor.name,message:error.message})}", stage)
        for _ in range(1000):
            result = page.evaluate("window.fixtureResult")
            if result is not None:
                return result
            message = page.evaluate("fixtureInputs.shift()")
            if message:
                point = {"x": message["x"], "y": message["y"]}
                if message["type"] == "input-wheel":
                    page.fixture_cdp.send("Input.dispatchMouseEvent", {"type": "mouseWheel", **point,
                                          "deltaX": message["deltaX"], "deltaY": message["deltaY"]})
                elif message["type"] == "input-move":
                    page.fixture_cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", **point})
                elif message["type"] == "input-click":
                    for event in ["mousePressed", "mouseReleased"]:
                        page.fixture_cdp.send("Input.dispatchMouseEvent", {"type": event, **point,
                                              "button": "left", "clickCount": 1})
                else:
                    self.fail(message["type"])
                page.evaluate("resolveInput({ok:true,sent:true})")
            page.wait_for_timeout(20)
        self.fail("Interaction did not settle")

    def assert_click_without_scrolling(self, page):
        self.assertEqual(self.click(page), {"value": True})
        self.assertEqual(page.evaluate("window.clicked"), 1)
        self.assertTrue(page.evaluate("window.trustedClick"))
        self.assertFalse(page.evaluate("testMessages.some(m => m.type === 'input-wheel' || m.type === 'pause')"))

    def test_absolute_control_escapes_zero_height_overflow_ancestor(self):
        page = self.fixture('<div style="height:0;overflow:hidden">'
                            '<button id="target" style="position:absolute;top:200px;left:100px">Continue</button></div>')
        self.assert_click_without_scrolling(page)

    def test_top_layer_dialog_escapes_transformed_clipping_ancestor(self):
        page = self.fixture('<div style="height:0;overflow:hidden;transform:translateZ(0)">'
                            '<dialog><button id="target">Continue</button></dialog></div>'
                            '<script>document.querySelector("dialog").showModal()</script>')
        self.assert_click_without_scrolling(page)

    def test_partly_visible_button_does_not_scroll_an_already_clickable_page(self):
        page = self.fixture('<div style="margin:80px;height:55px;overflow:hidden">'
                            '<button id="target" style="height:120px">Continue</button></div>')
        self.assert_click_without_scrolling(page)

    def test_narrow_visible_strip_uses_actual_clipped_area_for_hit_testing(self):
        page = self.fixture('<div style="margin:80px;height:18px;overflow:clip">'
                            '<button id="target" style="height:120px">Continue</button></div>')
        self.assert_click_without_scrolling(page)

    def test_replacement_during_wheel_is_retried_within_same_wait(self):
        page = self.fixture('<div style="height:1000px"></div><button id="target">Continue</button>'
                            '<div style="height:300px"></div>')
        page.evaluate("""() => {
            const send=chrome.runtime.sendMessage;
            chrome.runtime.sendMessage=async message=>{
                const result=await send(message);
                if(message.type==='input-wheel' && !window.replaced) {
                    window.replaced=true;
                    const old=document.querySelector('#target'), fresh=old.cloneNode(true);
                    fresh.onclick=old.onclick; old.replaceWith(fresh);
                }
                return result;
            };
        }""")
        self.assertEqual(self.click(page, 'email'), {'value': True})
        self.assertTrue(page.evaluate('window.replaced'))
        self.assertEqual(page.evaluate('window.clicked'), 1)
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='pause')"))

    def test_offscreen_control_still_scrolls_before_trusted_click(self):
        page = self.fixture('<div style="height:1000px"></div><button id="target">Continue</button>'
                            '<div style="height:300px"></div>')
        self.assertEqual(self.click(page), {"value": True})
        self.assertTrue(page.evaluate("testMessages.some(m => m.type === 'input-wheel')"))
        self.assertTrue(page.evaluate("window.trustedClick"))
        self.assertEqual(page.evaluate("window.clicked"), 1)

    def test_covered_control_waits_and_never_clicks_overlay(self):
        page = self.fixture('<button id="target">Continue</button>'
                            '<div style="position:fixed;inset:0;background:white" onclick="window.wrongClick=true"></div>')
        self.assertEqual(self.click(page), {"value": False})
        self.assertFalse(page.evaluate("!!window.clicked || !!window.wrongClick"))
        self.assertEqual(page.evaluate("testState.pauseCode"), "button_unavailable")

    def test_pause_during_scroll_prevents_click(self):
        page = self.fixture('<div style="height:1000px"></div><button id="target">Continue</button>')
        page.evaluate("""() => {
            const send = chrome.runtime.sendMessage;
            chrome.runtime.sendMessage = async message => {
                const result = await send(message);
                if (message.type === 'input-wheel') testState.active = false;
                return result;
            };
        }""")
        self.assertEqual(self.click(page)["error"], "StopStep")
        self.assertFalse(page.evaluate("!!window.clicked"))


if __name__ == '__main__':
    unittest.main(verbosity=2)
