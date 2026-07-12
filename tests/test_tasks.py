import sys
import types
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, call, patch

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

from core import tasks


@contextmanager
def applied_patches(*patchers):
    with ExitStack() as stack:
        yield [stack.enter_context(patcher) for patcher in patchers]


class FakeTextNode:
    def __init__(self, text="", visible=True):
        self.text = text
        self.visible = visible

    def is_visible(self, timeout=None):
        return self.visible

    def inner_text(self, timeout=None):
        return self.text


class FakeLocator:
    def __init__(self, items=None):
        self.items = list(items or [])

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeBubble(FakeTextNode):
    def __init__(self, text, state="success", rejection_tip=""):
        super().__init__(text=text)
        self.state = state
        self.rejection_tip = rejection_tip
        self.attributes = {}

    def evaluate(self, expression, marker):
        self.attributes[tasks.SEND_BASELINE_ATTRIBUTE] = marker

    def get_attribute(self, name):
        return self.attributes.get(name)

    def locator(self, selector):
        if selector == tasks.OUTGOING_MESSAGE_TEXT_SELECTOR:
            return FakeLocator([FakeTextNode(self.text)])
        if selector == tasks.OUTGOING_MESSAGE_STATUS_SELECTOR:
            if self.state in ("pending", "failed"):
                return FakeLocator([FakeTextNode(self.state)])
            return FakeLocator()
        if selector == tasks.OUTGOING_MESSAGE_PENDING_SELECTOR:
            if self.state == "pending":
                return FakeLocator([FakeTextNode("sending")])
            return FakeLocator()
        if selector.startswith("xpath=following-sibling"):
            if self.rejection_tip:
                return FakeLocator([FakeTextNode(self.rejection_tip)])
            return FakeLocator()
        raise AssertionError(f"unexpected bubble selector: {selector}")


class FakePage:
    def __init__(self, bubbles=None, rejection_tips=None):
        self.bubbles = list(bubbles or [])
        self.rejection_tips = list(rejection_tips or [])

    def locator(self, selector):
        if selector == tasks.OUTGOING_MESSAGE_SELECTOR:
            return FakeLocator(self.bubbles)
        if selector == tasks.CHAT_REJECTION_TIP_SELECTOR:
            return FakeLocator(
                FakeTextNode(text) for text in self.rejection_tips
            )
        raise AssertionError(f"unexpected page selector: {selector}")


class MessageConfirmationTests(unittest.TestCase):
    def test_cleared_input_without_new_bubble_is_not_success(self):
        page = FakePage()
        snapshot = tasks.capture_message_send_snapshot(page, "hello")

        with self.assertRaisesRegex(
            tasks.MessageSendNotConfirmed,
            "未检测到新增的本人消息气泡",
        ):
            tasks.wait_for_message_send_confirmation(
                page,
                snapshot,
                "hello",
                timeout=5,
                poll_interval=0.001,
                stable_seconds=0,
            )

    def test_pending_message_can_transition_to_success(self):
        states = [
            {"state": "pending", "detail": "消息仍在发送中"},
            {"state": "success", "detail": "消息气泡已进入成功状态"},
        ]

        with applied_patches(
            patch.object(
                tasks,
                "inspect_new_outgoing_message",
                side_effect=states,
            ),
            patch.object(tasks.time, "sleep", return_value=None),
        ):
            result = tasks.wait_for_message_send_confirmation(
                object(),
                {},
                "hello",
                timeout=100,
                stable_seconds=0,
            )

        self.assertEqual(result["state"], "success")

    def test_success_must_remain_stable_before_returning(self):
        states = [
            {"state": "success", "detail": "消息气泡已进入成功状态"},
            {"state": "rejected", "detail": "延迟出现审核提示"},
        ]

        with applied_patches(
            patch.object(
                tasks,
                "inspect_new_outgoing_message",
                side_effect=states,
            ),
            patch.object(
                tasks.time,
                "monotonic",
                side_effect=[0.0, 0.0, 0.0, 0.5],
            ),
            patch.object(tasks.time, "sleep", return_value=None),
        ):
            with self.assertRaisesRegex(
                tasks.MessageSendNotConfirmed,
                "延迟出现审核提示",
            ):
                tasks.wait_for_message_send_confirmation(
                    object(),
                    {},
                    "hello",
                    timeout=1000,
                    stable_seconds=1.0,
                )

    def test_transient_dom_error_is_not_treated_as_success(self):
        states = [
            RuntimeError("locator detached"),
            {"state": "success", "detail": "消息气泡已进入成功状态"},
        ]

        with applied_patches(
            patch.object(
                tasks,
                "inspect_new_outgoing_message",
                side_effect=states,
            ),
            patch.object(tasks.time, "sleep", return_value=None),
        ):
            result = tasks.wait_for_message_send_confirmation(
                object(),
                {},
                "hello",
                timeout=100,
                stable_seconds=0,
            )

        self.assertEqual(result["state"], "success")

    def test_failed_status_is_rejected(self):
        page = FakePage()
        snapshot = tasks.capture_message_send_snapshot(page, "hello")
        page.bubbles.append(FakeBubble("hello", state="failed"))

        with self.assertRaisesRegex(
            tasks.MessageSendNotConfirmed,
            "发送失败状态",
        ):
            tasks.wait_for_message_send_confirmation(
                page,
                snapshot,
                "hello",
                timeout=100,
                stable_seconds=0,
            )

    def test_repeated_text_uses_the_unmarked_new_bubble(self):
        old_bubble = FakeBubble("hello", state="success")
        page = FakePage([old_bubble])
        snapshot = tasks.capture_message_send_snapshot(page, "hello")
        new_bubble = FakeBubble("hello", state="failed")
        page.bubbles.insert(0, new_bubble)

        state = tasks.inspect_new_outgoing_message(page, snapshot, "hello")

        self.assertEqual(state["state"], "failed")

    def test_rendered_emoji_images_can_omit_bracket_codes(self):
        page = FakePage()
        snapshot = tasks.capture_message_send_snapshot(
            page,
            "[盖瑞]今日火花[加一]",
        )
        page.bubbles.append(FakeBubble("今日火花", state="success"))

        state = tasks.inspect_new_outgoing_message(
            page,
            snapshot,
            "[盖瑞]今日火花[加一]",
        )

        self.assertEqual(state["state"], "success")

    def test_moderation_tip_is_rejected(self):
        page = FakePage()
        snapshot = tasks.capture_message_send_snapshot(page, "hello")
        page.bubbles.append(
            FakeBubble("hello", rejection_tip="该消息涉及敏感内容")
        )

        with self.assertRaisesRegex(
            tasks.MessageSendNotConfirmed,
            "敏感内容",
        ):
            tasks.wait_for_message_send_confirmation(
                page,
                snapshot,
                "hello",
                timeout=100,
                stable_seconds=0,
            )

    def test_send_message_does_not_fall_back_to_input_cleared(self):
        page = MagicMock()
        chat_input = MagicMock()

        with applied_patches(
            patch.object(tasks.time, "sleep", return_value=None),
            patch.object(tasks, "close_popups_and_guides"),
            patch.object(tasks, "wait_chat_input_ready", return_value=True),
            patch.object(tasks, "find_chat_input", return_value=chat_input),
            patch.object(tasks, "type_message_with_real_events", return_value="fill"),
            patch.object(tasks, "get_editable_text", return_value="hello"),
            patch.object(
                tasks,
                "capture_message_send_snapshot",
                return_value={
                    "bubble_count": 0,
                    "matching_count": 0,
                    "rejection_tip_count": 0,
                },
            ),
            patch.object(
                tasks,
                "click_send_or_press_enter",
                return_value="mouse_button",
            ),
            patch.object(
                tasks,
                "wait_for_message_send_confirmation",
                side_effect=tasks.MessageSendNotConfirmed("没有新增气泡"),
            ),
            patch.object(tasks, "wait_input_cleared"),
            patch.object(tasks, "save_debug_page"),
        ) as patched:
            input_cleared = patched[-2]

            with self.assertRaisesRegex(RuntimeError, "没有新增气泡"):
                tasks.send_message_to_friend(
                    page,
                    "account",
                    "friend",
                    "hello",
                )

        input_cleared.assert_not_called()


class SingleSendActionTests(unittest.TestCase):
    def test_actionable_button_is_clicked_once_without_enter(self):
        page = MagicMock()
        button = MagicMock()
        chat_input = MagicMock()
        button.click.return_value = None

        with applied_patches(
            patch.object(tasks.time, "sleep", return_value=None),
            patch.object(tasks, "find_send_button", return_value=button),
            patch.object(tasks, "is_send_button_enabled", return_value=True),
            patch.object(tasks, "get_button_debug_info", return_value="{}"),
            patch.object(tasks, "get_editable_text", return_value="hello"),
        ):
            method = tasks.click_send_or_press_enter(
                page,
                "account",
                "friend",
                chat_input=chat_input,
                expected_message="hello",
            )

        self.assertEqual(method, "locator_button")
        self.assertEqual(
            button.click.call_args_list,
            [call(trial=True, timeout=3000), call(timeout=5000)],
        )
        chat_input.press.assert_not_called()
        page.mouse.assert_not_called()

    def test_obscured_button_uses_enter_as_the_only_real_action(self):
        page = MagicMock()
        button = MagicMock()
        chat_input = MagicMock()
        button.click.side_effect = tasks.PlaywrightTimeoutError("covered")

        with applied_patches(
            patch.object(tasks.time, "sleep", return_value=None),
            patch.object(tasks, "find_send_button", return_value=button),
            patch.object(tasks, "is_send_button_enabled", return_value=True),
            patch.object(tasks, "get_button_debug_info", return_value="{}"),
            patch.object(tasks, "get_editable_text", return_value="hello"),
        ):
            method = tasks.click_send_or_press_enter(
                page,
                "account",
                "friend",
                chat_input=chat_input,
                expected_message="hello",
            )

        self.assertEqual(method, "enter")
        button.click.assert_called_once_with(trial=True, timeout=3000)
        chat_input.press.assert_called_once_with("Enter", timeout=5000)
        page.mouse.assert_not_called()

    def test_real_click_exception_never_falls_back_to_enter(self):
        page = MagicMock()
        button = MagicMock()
        chat_input = MagicMock()
        button.click.side_effect = [None, RuntimeError("detached after click")]

        with applied_patches(
            patch.object(tasks.time, "sleep", return_value=None),
            patch.object(tasks, "find_send_button", return_value=button),
            patch.object(tasks, "is_send_button_enabled", return_value=True),
            patch.object(tasks, "get_button_debug_info", return_value="{}"),
            patch.object(tasks, "get_editable_text", return_value="hello"),
        ):
            method = tasks.click_send_or_press_enter(
                page,
                "account",
                "friend",
                chat_input=chat_input,
                expected_message="hello",
            )

        self.assertEqual(method, "locator_button_ambiguous")
        self.assertEqual(button.click.call_count, 2)
        chat_input.press.assert_not_called()
        page.mouse.assert_not_called()

    def test_enter_exception_never_attempts_a_real_button_click(self):
        page = MagicMock()
        button = MagicMock()
        chat_input = MagicMock()
        button.click.side_effect = tasks.PlaywrightTimeoutError("covered")
        chat_input.press.side_effect = RuntimeError("editor detached")

        with applied_patches(
            patch.object(tasks.time, "sleep", return_value=None),
            patch.object(tasks, "find_send_button", return_value=button),
            patch.object(tasks, "is_send_button_enabled", return_value=True),
            patch.object(tasks, "get_button_debug_info", return_value="{}"),
            patch.object(tasks, "get_editable_text", return_value="hello"),
        ):
            method = tasks.click_send_or_press_enter(
                page,
                "account",
                "friend",
                chat_input=chat_input,
                expected_message="hello",
            )

        self.assertEqual(method, "enter_ambiguous")
        button.click.assert_called_once_with(trial=True, timeout=3000)
        chat_input.press.assert_called_once_with("Enter", timeout=5000)
        page.mouse.assert_not_called()

    def test_non_actionability_trial_error_aborts_without_enter(self):
        page = MagicMock()
        button = MagicMock()
        chat_input = MagicMock()
        button.click.side_effect = RuntimeError("page crashed")

        with applied_patches(
            patch.object(tasks.time, "sleep", return_value=None),
            patch.object(tasks, "find_send_button", return_value=button),
            patch.object(tasks, "is_send_button_enabled", return_value=True),
            patch.object(tasks, "get_editable_text", return_value="hello"),
        ):
            method = tasks.click_send_or_press_enter(
                page,
                "account",
                "friend",
                chat_input=chat_input,
                expected_message="hello",
            )

        self.assertIsNone(method)
        button.click.assert_called_once_with(trial=True, timeout=3000)
        chat_input.press.assert_not_called()

    def test_input_changed_during_trial_prevents_real_action(self):
        page = MagicMock()
        button = MagicMock()
        chat_input = MagicMock()

        with applied_patches(
            patch.object(tasks.time, "sleep", return_value=None),
            patch.object(tasks, "find_send_button", return_value=button),
            patch.object(tasks, "is_send_button_enabled", return_value=True),
            patch.object(tasks, "get_button_debug_info", return_value="{}"),
            patch.object(
                tasks,
                "get_editable_text",
                side_effect=["hello", "changed"],
            ),
        ):
            method = tasks.click_send_or_press_enter(
                page,
                "account",
                "friend",
                chat_input=chat_input,
                expected_message="hello",
            )

        self.assertIsNone(method)
        button.click.assert_called_once_with(trial=True, timeout=3000)
        chat_input.press.assert_not_called()

    def test_changed_input_prevents_every_send_action(self):
        page = MagicMock()
        button = MagicMock()
        chat_input = MagicMock()

        with applied_patches(
            patch.object(tasks.time, "sleep", return_value=None),
            patch.object(tasks, "find_send_button", return_value=button),
            patch.object(tasks, "is_send_button_enabled", return_value=True),
            patch.object(tasks, "get_button_debug_info", return_value="{}"),
            patch.object(tasks, "get_editable_text", return_value="different"),
        ):
            method = tasks.click_send_or_press_enter(
                page,
                "account",
                "friend",
                chat_input=chat_input,
                expected_message="hello",
            )

        self.assertIsNone(method)
        button.click.assert_not_called()
        chat_input.press.assert_not_called()
        page.mouse.assert_not_called()


class TargetDiscoveryTests(unittest.TestCase):
    def test_reaching_bottom_with_missing_target_fails_account(self):
        page = MagicMock()
        first_friend = MagicMock()

        def locate(selector):
            locator = MagicMock()

            if "no-more-tip-" in selector:
                locator.count.return_value = 1
            else:
                locator.all.return_value = []

            return locator

        page.locator.side_effect = locate

        with applied_patches(
            patch.object(tasks.time, "sleep", return_value=None),
            patch.object(tasks, "check_page_status"),
            patch.object(tasks, "close_popups_and_guides"),
            patch.object(
                tasks,
                "get_first_visible_locator",
                return_value=first_friend,
            ),
            patch.object(tasks, "save_found_friends"),
            patch.object(tasks, "save_debug_page"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "仍有以下好友未找到: missing-friend",
            ):
                list(
                    tasks.scroll_and_select_user(
                        page,
                        "account",
                        ["missing-friend"],
                    )
                )

    def test_direct_click_cannot_hide_other_missing_targets(self):
        page = MagicMock()

        with applied_patches(
            patch.object(tasks.time, "sleep", return_value=None),
            patch.object(tasks, "check_page_status"),
            patch.object(tasks, "close_popups_and_guides"),
            patch.object(tasks, "get_first_visible_locator", return_value=None),
            patch.object(
                tasks,
                "click_target_by_text_directly",
                return_value=("friend-a", "target-a"),
            ),
            patch.object(tasks, "save_found_friends"),
            patch.object(tasks, "save_debug_page"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "仍有以下好友未找到: target-b",
            ):
                list(
                    tasks.scroll_and_select_user(
                        page,
                        "account",
                        ["target-a", "target-b"],
                    )
                )


class RunTasksTests(unittest.TestCase):
    def setUp(self):
        self.playwright = MagicMock()
        self.browser = MagicMock()
        self.users = [
            {
                "username": "first",
                "unique_id": "1",
                "cookies": [],
                "targets": ["friend-1"],
            },
            {
                "username": "second",
                "unique_id": "2",
                "cookies": [],
                "targets": ["friend-2"],
            },
        ]

    def test_account_failure_continues_then_fails_process(self):
        with applied_patches(
            patch.object(
                tasks,
                "get_browser",
                return_value=(self.playwright, self.browser),
            ),
            patch.object(tasks, "userData", self.users),
            patch.object(
                tasks,
                "do_user_task",
                side_effect=[RuntimeError("send failed"), None],
            ),
        ) as patched:
            do_user_task = patched[-1]

            with self.assertRaisesRegex(
                RuntimeError,
                "1 个账号任务失败: first: RuntimeError: send failed",
            ):
                tasks.runTasks()

        self.assertEqual(do_user_task.call_count, 2)
        self.assertEqual(
            do_user_task.call_args_list,
            [
                call(self.browser, "first", [], ["friend-1"]),
                call(self.browser, "second", [], ["friend-2"]),
            ],
        )
        self.browser.close.assert_called_once_with()
        self.playwright.stop.assert_called_once_with()

    def test_all_accounts_succeed(self):
        with applied_patches(
            patch.object(
                tasks,
                "get_browser",
                return_value=(self.playwright, self.browser),
            ),
            patch.object(tasks, "userData", self.users),
            patch.object(tasks, "do_user_task"),
        ) as patched:
            do_user_task = patched[-1]
            tasks.runTasks()

        self.assertEqual(do_user_task.call_count, 2)
        self.browser.close.assert_called_once_with()
        self.playwright.stop.assert_called_once_with()

    def test_cleanup_error_does_not_mask_account_failure(self):
        self.browser.close.side_effect = RuntimeError("close failed")

        with applied_patches(
            patch.object(
                tasks,
                "get_browser",
                return_value=(self.playwright, self.browser),
            ),
            patch.object(tasks, "userData", self.users[:1]),
            patch.object(
                tasks,
                "do_user_task",
                side_effect=RuntimeError("send failed"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "first: RuntimeError: send failed",
            ):
                tasks.runTasks()

        self.playwright.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
