import sys
import types
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

try:
    import playwright.sync_api  # noqa: F401
except ModuleNotFoundError:
    playwright_module = types.ModuleType("playwright")
    sync_api_module = types.ModuleType("playwright.sync_api")
    sync_api_module.Response = type("Response", (), {})
    sync_api_module.TimeoutError = type("TimeoutError", (Exception,), {})
    sync_api_module.sync_playwright = lambda: None
    playwright_module.sync_api = sync_api_module
    sys.modules["playwright"] = playwright_module
    sys.modules["playwright.sync_api"] = sync_api_module

from core import browser as browser_module


class GetBrowserTests(unittest.TestCase):
    def setUp(self):
        self.playwright = MagicMock()
        self.playwright_manager = MagicMock()
        self.playwright_manager.start.return_value = self.playwright
        self.browser = MagicMock()
        self.guard = MagicMock()
        self.guard.launch_argument = "--dysf-run-token=test-token"
        self.guard.capture_before_launch.return_value = None
        owned_process = MagicMock()
        owned_process.pid = 123
        self.guard.capture_after_launch.return_value = [owned_process]
        self.guard.capture_before_close.return_value = []
        self.guard.cleanup.return_value = []

    def common_patches(self):
        return (
            patch.object(
                browser_module,
                "sync_playwright",
                return_value=self.playwright_manager,
            ),
            patch.object(
                browser_module,
                "ChromeProcessGuard",
                return_value=self.guard,
            ),
            patch.object(
                browser_module,
                "find_system_chrome",
                return_value=r"C:\Chrome\chrome.exe",
            ),
            patch.object(
                browser_module,
                "should_run_headless",
                return_value=True,
            ),
            patch.object(browser_module.traceback, "print_exc"),
        )

    def test_success_returns_guard_and_includes_tracking_argument(self):
        self.playwright.chromium.launch.return_value = self.browser

        with ExitStack() as stack:
            for patcher in self.common_patches():
                stack.enter_context(patcher)
            result = browser_module.get_browser()

        self.assertEqual(result, (self.playwright, self.browser, self.guard))
        launch_args = self.playwright.chromium.launch.call_args.kwargs["args"]
        self.assertIn(self.guard.launch_argument, launch_args)
        self.guard.capture_before_launch.assert_called_once_with()
        self.guard.capture_after_launch.assert_called_once_with()

    def test_snapshot_happens_before_chrome_launch(self):
        events = []
        self.guard.capture_before_launch.side_effect = lambda: events.append(
            "snapshot"
        )

        def launch(**kwargs):
            events.append("launch")
            return self.browser

        self.playwright.chromium.launch.side_effect = launch

        with ExitStack() as stack:
            for patcher in self.common_patches():
                stack.enter_context(patcher)
            browser_module.get_browser()

        self.assertEqual(events, ["snapshot", "launch"])

    def test_snapshot_error_prevents_chrome_launch(self):
        self.guard.capture_before_launch.side_effect = RuntimeError(
            "snapshot failed"
        )

        with ExitStack() as stack:
            for patcher in self.common_patches():
                stack.enter_context(patcher)
            with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
                browser_module.get_browser()

        self.playwright.chromium.launch.assert_not_called()
        self.guard.capture_before_close.assert_called_once_with()
        self.guard.cleanup.assert_called_once_with()
        self.playwright.stop.assert_called_once_with()

    def test_launch_error_without_browser_still_attempts_guard_cleanup(self):
        self.playwright.chromium.launch.side_effect = RuntimeError("launch failed")

        with ExitStack() as stack:
            for patcher in self.common_patches():
                stack.enter_context(patcher)
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                browser_module.get_browser()

        self.guard.capture_before_close.assert_called_once_with()
        self.guard.capture_before_launch.assert_called_once_with()
        self.guard.cleanup.assert_called_once_with()
        self.playwright.stop.assert_called_once_with()

    def test_capture_error_closes_browser_then_cleans_processes(self):
        self.playwright.chromium.launch.return_value = self.browser
        self.guard.capture_after_launch.side_effect = RuntimeError(
            "capture failed"
        )

        with ExitStack() as stack:
            for patcher in self.common_patches():
                stack.enter_context(patcher)
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                browser_module.get_browser()

        self.guard.capture_before_close.assert_called_once_with()
        self.guard.capture_before_launch.assert_called_once_with()
        self.browser.close.assert_called_once_with()
        self.guard.cleanup.assert_called_once_with()
        self.playwright.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
