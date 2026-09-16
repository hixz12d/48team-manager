"""Account environments must survive retries, rotations and browser restarts."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.integrations.openai.browser import environment as env
from app.integrations.openai.browser import onboard, reauth


class BrowserEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / "chrome.exe"
        self.binary.touch()
        self.settings = Settings(_env_file=None, browser_engine="chromix",
                                 browser_executable=str(self.binary), browser_channel="chrome",
                                 browser_locale="en-US", browser_timezone="America/Los_Angeles")
        self.proxy = {"server": "socks5://127.0.0.1:1080", "bypass": "<-loopback>"}

    def launch(self, account="kid_at_example.com", settings=None, **kwargs):
        return env.context_options(self.root / account, self.proxy,
                                   settings=settings or self.settings, **kwargs)

    def manifest(self, options):
        return Path(options["user_data_dir"]) / env.PROFILE_FILE

    def test_one_account_keeps_environment_after_retry_and_config_change(self):
        initial = self.launch()
        saved = self.manifest(initial).read_bytes()
        changed = self.settings.model_copy(update={"browser_locale": "de-DE", "browser_timezone": "Europe/Berlin"})
        with patch.object(env.secrets, "randbits", side_effect=AssertionError("must reuse seed")):
            repeated = self.launch(settings=changed)
        self.assertEqual(initial, repeated)
        self.assertEqual(saved, self.manifest(repeated).read_bytes())

    def test_different_accounts_get_independent_persistent_environments(self):
        with (patch.object(env.secrets, "randbits", side_effect=[101, 202]),
              patch.object(env.secrets, "choice", side_effect=[(1200, 720), (1280, 800)])):
            first, second = self.launch("one"), self.launch("two")
        self.assertNotEqual(first["args"], second["args"])
        self.assertNotEqual(first["viewport"], second["viewport"])
        self.assertEqual(first, self.launch("one"))
        self.assertEqual(second, self.launch("two"))
        self.assertNotEqual(first["user_data_dir"], second["user_data_dir"])

    def test_concurrent_first_launches_publish_one_complete_manifest(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.launch(), range(16)))
        self.assertTrue(all(item == results[0] for item in results))
        directory = Path(results[0]["user_data_dir"])
        self.assertEqual([p.name for p in directory.iterdir()], [env.PROFILE_FILE])

    def test_corrupt_or_changed_platform_never_silently_rotates(self):
        options = self.launch()
        manifest = self.manifest(options)
        original = json.loads(manifest.read_text())
        broken = ["{", "[]", json.dumps({**original, "seed": 0}),
                  json.dumps({**original, "seed": True}), json.dumps({**original, "schema": 2}),
                  json.dumps({**original, "platform": "not-" + sys.platform}),
                  json.dumps({**original, "viewport": {"width": -1, "height": 800}}),
                  json.dumps({**original, "timezone": "Earth/Invalid"})]
        for content in broken:
            with self.subTest(content=content):
                manifest.write_text(content, encoding="utf-8")
                with self.assertRaises(env.BrowserEnvironmentError):
                    self.launch()
                self.assertEqual(content, manifest.read_text())

    def test_missing_manifest_with_browser_data_is_not_recreated(self):
        options = self.launch()
        manifest = self.manifest(options)
        (manifest.parent / "Default").mkdir()
        manifest.unlink()
        with self.assertRaises(env.BrowserEnvironmentError):
            self.launch()
        self.assertFalse(manifest.exists())

    def test_chromix_does_not_overwrite_existing_chromium_profile(self):
        original = self.root / "kid_at_example.com"
        original.mkdir()
        (original / "Local State").write_text("original cookies/settings")
        chromix = self.launch()
        chromium = self.launch(settings=self.settings.model_copy(update={"browser_engine": "chromium", "browser_executable": ""}))
        self.assertNotEqual(chromix["user_data_dir"], chromium["user_data_dir"])
        self.assertEqual(Path(chromium["user_data_dir"]), original)
        self.assertEqual((original / "Local State").read_text(), "original cookies/settings")
        self.assertEqual(chromium["channel"], "chrome")
        self.assertFalse((original / env.PROFILE_FILE).exists())

    def test_missing_binary_and_launcher_are_rejected_before_creating_profile(self):
        for binary in ("", str(self.root / "missing"), str(self.root / "chromix.cmd")):
            with self.subTest(binary=binary):
                if binary.endswith(".cmd"):
                    Path(binary).touch()
                settings = self.settings.model_copy(update={"browser_executable": binary})
                with self.assertRaises(env.BrowserEnvironmentError):
                    self.launch(settings=settings)
        self.assertFalse((self.root / "chromix").exists())

    def test_override_precedes_default_and_does_not_select_channel(self):
        other = self.root / "other-chrome.exe"
        other.touch()
        result = self.launch(executable_path=str(other))
        self.assertEqual(result["executable_path"], str(other))
        self.assertNotIn("channel", result)
        self.assertEqual(result["proxy"], self.proxy)
        self.assertEqual(result["timezone_id"], "America/Los_Angeles")
        self.assertIn("--fingerprint-timezone=America/Los_Angeles", result["args"])
        self.assertIn("--disable-gpu-fingerprint", result["args"])
        self.assertFalse(any(a.startswith(("--fingerprint-platform=", "--fingerprint-gpu-renderer=",
                                          "--fingerprint-hardware-concurrency=", "--fingerprint-webrtc-ip="))
                             for a in result["args"]))

    def test_summary_does_not_expose_account_proxy_path_or_seed(self):
        result = self.launch()
        seed = json.loads(self.manifest(result).read_text())["seed"]
        summary = env.environment_summary(result)
        self.assertIn("Chromix env=", summary)
        for private in (str(seed), str(self.root), "kid_at_example.com", "127.0.0.1", str(self.binary)):
            self.assertNotIn(private, summary)
        self.assertEqual(summary, env.environment_summary(self.launch()))

    def test_registration_and_standalone_reauth_use_same_configured_browser(self):
        # Reauth must honor BROWSER_EXECUTABLE even without a CLI override.
        with (patch.object(onboard, "settings", self.settings),
              patch.object(onboard, "ensure_virtual_display")):
            initial = onboard.chromium_context_kwargs(self.root / "kid", self.proxy)
            resumed = reauth.chromium_context_kwargs(self.root / "kid", self.proxy)
        self.assertEqual(initial, resumed)
        self.assertEqual(resumed["executable_path"], str(self.binary))
        self.assertNotIn("channel", resumed)

    def test_invalid_region_does_not_write_manifest(self):
        for update in ({"browser_locale": "en-US\n--flag"}, {"browser_timezone": "Earth/Nowhere"}):
            with self.subTest(update=update):
                with self.assertRaises(env.BrowserEnvironmentError):
                    self.launch(settings=self.settings.model_copy(update=update))
        self.assertFalse((self.root / "chromix").exists())

    def test_empty_timezone_is_pinned_for_chromix(self):
        options = self.launch(settings=self.settings.model_copy(update={"browser_timezone": ""}))
        self.assertEqual(options["timezone_id"], "UTC")
        changed = self.settings.model_copy(update={"browser_timezone": "Europe/Paris"})
        self.assertEqual(self.launch(settings=changed)["timezone_id"], "UTC")


class BrowserEnvironmentPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_configuration_stops_before_hme_or_invitation(self):
        from unittest.mock import AsyncMock
        from app.application.onboard import OnboardService

        settings = Settings(_env_file=None, browser_engine="chromix", browser_executable="")
        with (patch("app.core.config.load_settings", return_value=settings),
              patch("app.application.onboard.hme_service.maybe_claim_alias", new_callable=AsyncMock) as claim,
              patch.object(OnboardService, "_invite_and_onboard_impl", new_callable=AsyncMock) as invite):
            result = await OnboardService().invite_and_onboard(
                None, workspace_id=1, email_line="", oauth_signup=True,
            )
        self.assertEqual(result["error_code"], "browser_environment_invalid")
        claim.assert_not_called()
        invite.assert_not_called()

    async def test_progress_persists_only_environment_summary_verbatim(self):
        from unittest.mock import AsyncMock, MagicMock
        from app.application.invitation_flow import browser_progress
        from app.application.operations import operation_store

        db = AsyncMock()
        op = MagicMock(cancel_requested=False)
        with (patch.object(operation_store, "get_by_public_id", new=AsyncMock(return_value=op)),
              patch.object(operation_store, "note", new_callable=AsyncMock) as note):
            await browser_progress(db, "job", "browser_environment", "Chromix env=abc viewport=1280x800")
            self.assertEqual(note.await_args.args[3], "Chromix env=abc viewport=1280x800")
            await browser_progress(db, "job", "wait_page", "https://example.invalid?secret=private")
            self.assertEqual(note.await_args.args[3], "浏览器阶段：wait_page")


if __name__ == "__main__":
    unittest.main()
