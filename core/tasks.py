import os
import re
import time
import json
import traceback
from collections import deque

from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
from playwright.sync_api import Response, TimeoutError as PlaywrightTimeoutError


complates = {}

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
userIDDict = {}


def safe_filename(name):
    """
    Convert an account name into a safe filename.
    """
    name = str(name or "unknown")
    return re.sub(r'[\\/:*?"<>|]+', "_", name)


def save_debug_page(page, name="debug"):
    """
    Save screenshot and HTML for debugging login, captcha, overlay, empty page,
    or page structure changes.
    """
    os.makedirs("debug", exist_ok=True)

    timestamp = int(time.time())
    filename = safe_filename(name)
    screenshot_path = f"debug/{filename}_{timestamp}.png"
    html_path = f"debug/{filename}_{timestamp}.html"

    try:
        logger.error(f"当前页面 URL: {page.url}")
        logger.error(f"当前页面标题: {page.title()}")
    except Exception:
        pass

    try:
        page.screenshot(path=screenshot_path, full_page=True)
        logger.error(f"已保存截图: {screenshot_path}")
    except Exception:
        logger.exception("保存截图失败")

    try:
        html = page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.error(f"已保存 HTML: {html_path}")
    except Exception:
        logger.exception("保存 HTML 失败")


def save_found_friends(account_username, found_targets):
    """
    Save all friend names scanned in this run.
    """
    os.makedirs("debug", exist_ok=True)

    filename = safe_filename(account_username)
    path = f"debug/{filename}_found_friends.txt"

    try:
        with open(path, "w", encoding="utf-8") as f:
            for name in sorted(found_targets):
                f.write(str(name) + "\n")

        logger.info(f"账号 {account_username} 已保存扫描到的好友列表: {path}")

    except Exception:
        logger.exception(f"账号 {account_username} 保存好友列表失败")


def raise_if_targets_missing(account_username, remaining_targets):
    missing_targets = sorted(str(target) for target in remaining_targets if target)

    if not missing_targets:
        return

    raise RuntimeError(
        f"账号 {account_username} 搜索结束，仍有以下好友未找到: "
        f"{', '.join(missing_targets)}"
    )


def check_page_status(page, username):
    """
    Check whether the page may be in login/captcha/security verification state.
    """
    try:
        url = page.url
        title = page.title()
        html = page.content()
    except Exception:
        logger.exception(f"账号 {username} 获取页面状态失败")
        return

    logger.debug(f"账号 {username} 当前 URL: {url}")
    logger.debug(f"账号 {username} 当前标题: {title}")

    abnormal_keywords = [
        "登录",
        "扫码",
        "验证码",
        "安全验证",
        "passport",
        "captcha",
        "verify",
        "login",
    ]

    lower_url = str(url).lower()
    lower_html = str(html).lower()

    for keyword in abnormal_keywords:
        keyword_lower = keyword.lower()
        if keyword_lower in lower_url or keyword_lower in lower_html:
            logger.warning(f"账号 {username} 页面可能异常，检测到关键词: {keyword}")
            break


def close_popups_and_guides(page, account_username=""):
    """
    Close Douyin Creator Center onboarding guides, browser warnings, modal overlays,
    and other popups that may block clicks or the chat input.
    """

    click_selectors = [
        # Onboarding guide buttons
        "xpath=//button[contains(text(), '我知道了')]",
        "xpath=//button[contains(text(), '知道了')]",
        "xpath=//button[contains(text(), '跳过')]",
        "xpath=//button[contains(text(), '关闭')]",
        "xpath=//button[contains(text(), '继续使用')]",

        # Text fallback
        "xpath=//*[self::button or self::div or self::span][contains(text(), '我知道了')]",
        "xpath=//*[self::button or self::div or self::span][contains(text(), '知道了')]",
        "xpath=//*[self::button or self::div or self::span][contains(text(), '跳过')]",
        "xpath=//*[self::button or self::div or self::span][contains(text(), '关闭')]",

        # Shepherd guide
        "css=.shepherd-button",
        "css=.douyin-creator-pc-master__button-next",
        "css=.douyin-creator-pc-master__button-skip",

        # Browser check modal
        "css=.douyin-creator-browser-check-modal-btn-primary",
        "css=.douyin-creator-browser-check-modal-btn",

        # Semi modal
        "css=.semi-modal-close",
        "css=.semi-modal-close-icon",

        # Generic close
        "css=[aria-label='close']",
        "css=[aria-label='Close']",
        "css=[class*='close']",
    ]

    for _ in range(5):
        clicked = False

        for selector in click_selectors:
            try:
                locator = page.locator(selector)
                count = locator.count()

                if count == 0:
                    continue

                for i in range(count):
                    item = locator.nth(i)

                    try:
                        if item.is_visible():
                            logger.info(
                                f"账号 {account_username} 关闭弹窗/引导，选择器: {selector}，索引: {i}"
                            )
                            item.click(timeout=3000)
                            time.sleep(0.8)
                            clicked = True
                            break
                    except Exception:
                        continue

                if clicked:
                    break

            except Exception:
                continue

        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)
        except Exception:
            pass

        if not clicked:
            break

    # Fallback: hide common overlay/guide elements directly.
    try:
        page.evaluate(
            """
            () => {
                const selectors = [
                    '.shepherd-modal-overlay-container',
                    '.shepherd-element',
                    '.douyin-creator-browser-check-content',
                    '.douyin-creator-browser-check',
                    '.semi-modal-mask',
                    '.semi-modal-wrap',
                    '.semi-modal',
                    '[class*="mask-container"]',
                    '[class*="modal-mask"]',
                    '[class*="guide"]'
                ];

                for (const selector of selectors) {
                    document.querySelectorAll(selector).forEach(el => {
                        const text = (el.innerText || '').trim();
                        const className = String(el.className || '');

                        const shouldHide =
                            className.includes('shepherd') ||
                            className.includes('browser-check') ||
                            className.includes('modal') ||
                            className.includes('mask') ||
                            className.includes('guide') ||
                            text.includes('我知道了') ||
                            text.includes('跳过') ||
                            text.includes('继续使用');

                        if (shouldHide) {
                            el.style.display = 'none';
                            el.style.visibility = 'hidden';
                            el.style.pointerEvents = 'none';
                        }
                    });
                }

                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
            }
            """
        )
    except Exception:
        pass


def is_target_match(target_symbol, target_name, targets):
    """
    Match target friend by:
    1. exact match
    2. exact match after removing whitespace
    3. fuzzy containment match

    Returns:
        matched: bool
        matched_target: str | None
    """
    target_symbol = str(target_symbol or "").strip()
    target_name = str(target_name or "").strip()

    target_symbol_no_space = re.sub(r"\s+", "", target_symbol)
    target_name_no_space = re.sub(r"\s+", "", target_name)

    for target in targets:
        target = str(target or "").strip()

        if not target:
            continue

        target_no_space = re.sub(r"\s+", "", target)

        if target_symbol == target or target_name == target:
            return True, target

        if target_symbol_no_space == target_no_space:
            return True, target

        if target_name_no_space == target_no_space:
            return True, target

        if target_no_space in target_name_no_space or target_name_no_space in target_no_space:
            return True, target

        if target_no_space in target_symbol_no_space or target_symbol_no_space in target_no_space:
            return True, target

    return False, None


def handle_response(response: Response):
    """
    Listen only to the target user detail API response.
    """
    global userIDDict

    if "aweme/v1/creator/im/user_detail/" in response.url:
        try:
            json_data = response.json()

            for item in json_data.get("user_list", []):
                short_id = item.get("user", {}).get("ShortId")
                nickname = item.get("user", {}).get("nickname")
                user_id = item.get("user_id", "")

                if short_id:
                    userIDDict[str(short_id)] = {
                        "nickname": nickname,
                        "user_id": user_id,
                    }

        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            last = tb[-1]
            print(f"解析响应失败: {e}")
            print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    Generic retry helper.
    """
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)

        except Exception as e:
            if attempt < retries - 1:
                logger.warning(
                    f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}"
                )
                time.sleep(delay)

            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise


def wait_and_click(page, selector, username, desc, timeout=None):
    """
    Wait for a selector and click it. Save debug page when it fails.
    """
    if timeout is None:
        timeout = config["browserTimeout"]

    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout)
        close_popups_and_guides(page, username)
        page.locator(selector).click()
        return True

    except Exception:
        logger.error(f"账号 {username} 等待或点击失败: {desc}")
        check_page_status(page, username)
        save_debug_page(page, f"{username}_{desc}_failed")
        return False


def get_first_visible_locator(page, selectors, timeout=10000):
    """
    Return first visible locator from multiple selector candidates.
    """
    for selector in selectors:
        try:
            page.wait_for_selector(selector, state="visible", timeout=timeout)
            locator = page.locator(selector)

            if locator.count() > 0:
                return locator.first

        except Exception:
            continue

    return None


def xpath_literal(text):
    """
    Build a safe XPath string literal.
    """
    text = str(text)

    if "'" not in text:
        return f"'{text}'"

    if '"' not in text:
        return f'"{text}"'

    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def click_target_by_text_directly(page, account_username, targets):
    """
    Directly click target conversation by visible text.

    This is a fallback for pages where Douyin has changed the virtual list DOM
    and old semi-list-item selectors cannot find the first friend list item.
    """

    close_popups_and_guides(page, account_username)

    for target in targets:
        target_text = str(target or "").strip()

        if not target_text:
            continue

        literal = xpath_literal(target_text)

        selectors = [
            f"xpath=//*[normalize-space()={literal}]",
            f"xpath=//*[contains(normalize-space(), {literal})]",
        ]

        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = locator.count()

                logger.info(
                    f"账号 {account_username} 直接按文本查找目标好友，目标: {target_text}，选择器: {selector}，数量: {count}"
                )

                if count == 0:
                    continue

                for i in range(count):
                    item = locator.nth(i)

                    try:
                        if not item.is_visible(timeout=1000):
                            continue
                    except Exception:
                        continue

                    candidates = [
                        item,
                        item.locator("xpath=ancestor::li[1]"),
                        item.locator("xpath=ancestor::div[contains(@class, 'semi-list-item')][1]"),
                        item.locator("xpath=ancestor::div[contains(@class, 'list')][1]"),
                        item.locator("xpath=ancestor::div[contains(@class, 'item')][1]"),
                    ]

                    for candidate in candidates:
                        try:
                            if candidate.count() == 0:
                                continue

                            c = candidate.first

                            try:
                                if not c.is_visible(timeout=1000):
                                    continue
                            except Exception:
                                continue

                            c.scroll_into_view_if_needed(timeout=3000)
                            box = c.bounding_box()

                            if box:
                                x = box["x"] + box["width"] / 2
                                y = box["y"] + box["height"] / 2

                                page.mouse.click(x, y)
                                time.sleep(1.5)
                                close_popups_and_guides(page, account_username)

                                if chat_opened(page):
                                    logger.info(
                                        f"账号 {account_username} 直接按文本点击并打开聊天: {target_text}"
                                    )
                                    return target_text, target_text

                                page.mouse.dblclick(x, y)
                                time.sleep(1.5)
                                close_popups_and_guides(page, account_username)

                                if chat_opened(page):
                                    logger.info(
                                        f"账号 {account_username} 直接按文本双击并打开聊天: {target_text}"
                                    )
                                    return target_text, target_text

                            c.click(timeout=5000, force=True)
                            time.sleep(1.5)
                            close_popups_and_guides(page, account_username)

                            if chat_opened(page):
                                logger.info(
                                    f"账号 {account_username} 直接按文本 click 后打开聊天: {target_text}"
                                )
                                return target_text, target_text

                        except Exception:
                            continue

            except Exception:
                continue

    # If direct click failed in the current tab, try switching between 全部 and 群消息.
    for fallback_tab in ["全部", "群消息"]:
        try:
            literal_tab = xpath_literal(fallback_tab)
            tab_locator = page.locator(f"xpath=//*[@id='sub-app']//*[normalize-space()={literal_tab}]")

            if tab_locator.count() > 0:
                for tab_index in range(tab_locator.count()):
                    tab_item = tab_locator.nth(tab_index)

                    try:
                        if not tab_item.is_visible(timeout=1000):
                            continue
                    except Exception:
                        continue

                    tab_item.click(timeout=5000, force=True)
                    time.sleep(2)
                    close_popups_and_guides(page, account_username)
                    break

            for target in targets:
                target_text = str(target or "").strip()
                if not target_text:
                    continue

                literal = xpath_literal(target_text)
                locator = page.locator(f"xpath=//*[contains(normalize-space(), {literal})]")

                for i in range(locator.count()):
                    item = locator.nth(i)

                    try:
                        if not item.is_visible(timeout=1000):
                            continue
                    except Exception:
                        continue

                    box = item.bounding_box()

                    if box:
                        page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        time.sleep(1.5)

                        if chat_opened(page):
                            logger.info(
                                f"账号 {account_username} 切换 {fallback_tab} 后直接点击打开聊天: {target_text}"
                            )
                            return target_text, target_text

        except Exception:
            continue

    save_debug_page(page, f"{account_username}_direct_text_click_not_opened")
    return None


def get_preferred_message_tabs():
    """
    Decide which message tabs to try.

    Config examples:
      messageTab: all
      messageTab: group
      messageTab: friend
      messageTab: stranger

    Default is "all" because group chats can appear under both "全部" and "群消息".
    """

    raw = str(
        config.get(
            "messageTab",
            config.get("message_tab", config.get("privateMessageTab", "all")),
        )
    ).strip().lower()

    if raw in ["group", "groups", "group_message", "group_messages", "群消息", "群"]:
        return ["群消息", "全部"]

    if raw in ["friend", "friends", "friend_private", "朋友私信", "朋友"]:
        return ["朋友私信", "全部"]

    if raw in ["stranger", "strangers", "stranger_private", "陌生人私信", "陌生人"]:
        return ["陌生人私信", "全部"]

    # Default: "全部" first; if target is a group and 全部 DOM is weird, try 群消息 too.
    return ["全部", "群消息", "朋友私信", "陌生人私信"]


def select_message_tab(page, account_username):
    """
    Select the proper message tab before scanning conversations.

    The old code always clicked a fixed tab position, which may be wrong for
    group conversations. This function clicks by visible tab text.
    """

    close_popups_and_guides(page, account_username)

    tabs = get_preferred_message_tabs()

    for tab_name in tabs:
        literal = xpath_literal(tab_name)

        selectors = [
            f"xpath=//*[@id='sub-app']//*[normalize-space()={literal}]",
            f"xpath=//*[normalize-space()={literal}]",
            f"text={tab_name}",
        ]

        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = locator.count()

                logger.info(
                    f"账号 {account_username} 尝试切换私信标签: {tab_name}，选择器: {selector}，数量: {count}"
                )

                if count == 0:
                    continue

                for i in range(count):
                    item = locator.nth(i)

                    try:
                        if not item.is_visible(timeout=1000):
                            continue
                    except Exception:
                        continue

                    item.scroll_into_view_if_needed(timeout=3000)

                    try:
                        item.click(timeout=5000, force=True)
                    except Exception:
                        box = item.bounding_box()
                        if not box:
                            continue
                        page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )

                    time.sleep(2)
                    close_popups_and_guides(page, account_username)
                    logger.info(f"账号 {account_username} 已切换私信标签: {tab_name}")
                    return tab_name

            except Exception:
                continue

    logger.warning(
        f"账号 {account_username} 未能按文本切换私信标签，将继续在当前标签扫描"
    )
    save_debug_page(page, f"{account_username}_message_tab_select_failed")
    return None


def chat_opened(page):
    """
    Check whether a real chat detail pane is open.
    """
    selectors = [
        "css=div[class*='chat-input-'][contenteditable='true']",
        "css=div[class*='chat-editor-'] div[contenteditable='true']",
        "css=div[contenteditable='true']",
        "xpath=//div[contains(@class, 'chat-editor-')]",
        "xpath=//div[contains(@class, 'chat-input-')]",
    ]

    for selector in selectors:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue

    return False


def click_friend_element(page, element, account_username, target_name):
    """
    Click a friend list item.

    Some semi-list-item-body nodes are hidden virtual-list copies. Directly
    calling element.click() may wait until timeout because the resolved node is
    not visible. This function prefers visible friend name text and visible
    ancestors, then checks whether the chat detail pane is actually opened.
    """

    close_popups_and_guides(page, account_username)

    click_candidates = []

    # Prefer visible text node for the target friend.
    try:
        name_locator = page.locator(f"text={target_name}")

        for i in range(name_locator.count()):
            item = name_locator.nth(i)

            try:
                if item.is_visible(timeout=1000):
                    click_candidates.append(item)
                    click_candidates.append(item.locator("xpath=ancestor::li[1]"))
                    click_candidates.append(
                        item.locator("xpath=ancestor::div[contains(@class, 'semi-list-item')][1]")
                    )
                    click_candidates.append(
                        item.locator("xpath=ancestor::div[contains(@class, 'conversation')][1]")
                    )
            except Exception:
                continue
    except Exception:
        pass

    # Then try current element ancestors.
    click_candidates.extend([
        element.locator("xpath=ancestor::li[1]"),
        element.locator("xpath=ancestor::div[contains(@class, 'semi-list-item')][1]"),
        element,
    ])

    for candidate in click_candidates:
        try:
            if candidate.count() == 0:
                continue

            item = candidate.first

            try:
                if not item.is_visible(timeout=1000):
                    continue
            except Exception:
                continue

            item.scroll_into_view_if_needed(timeout=3000)
            box = item.bounding_box()

            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2

                page.mouse.click(x, y)
                time.sleep(1.5)
                close_popups_and_guides(page, account_username)

                if chat_opened(page):
                    logger.info(f"账号 {account_username} 已点击并打开聊天: {target_name}")
                    return True

                page.mouse.dblclick(x, y)
                time.sleep(1.5)
                close_popups_and_guides(page, account_username)

                if chat_opened(page):
                    logger.info(f"账号 {account_username} 双击后打开聊天: {target_name}")
                    return True

            item.click(timeout=5000, force=True)
            time.sleep(1.5)
            close_popups_and_guides(page, account_username)

            if chat_opened(page):
                logger.info(f"账号 {account_username} click 后打开聊天: {target_name}")
                return True

            try:
                page.keyboard.press("Enter")
                time.sleep(1.5)
                close_popups_and_guides(page, account_username)

                if chat_opened(page):
                    logger.info(f"账号 {account_username} Enter 后打开聊天: {target_name}")
                    return True
            except Exception:
                pass

        except Exception:
            continue

    # JS fallback.
    try:
        element.evaluate(
            """
            (el) => {
                const li = el.closest('li');
                const item = el.closest('[class*="semi-list-item"]');
                const target = li || item || el;
                target.scrollIntoView({ block: 'center' });
                target.click();
            }
            """
        )

        time.sleep(2)
        close_popups_and_guides(page, account_username)

        if chat_opened(page):
            logger.info(f"账号 {account_username} JS 点击后打开聊天: {target_name}")
            return True

    except Exception:
        pass

    logger.error(f"账号 {account_username} 点击好友后没有打开聊天详情: {target_name}")
    save_debug_page(page, f"{account_username}_{target_name}_chat_not_opened")
    return False


def find_chat_input(page, account_username, friend_name):
    """
    Find the chat input.

    Actual DOM example:
    <div class="chat-input-nSWBco" contenteditable="true"></div>
    """

    close_popups_and_guides(page, account_username)

    chat_input_selectors = [
        "css=div[class*='chat-input-'][contenteditable='true']",
        "css=div[class*='chat-editor-'] div[contenteditable='true']",
        "css=div[contenteditable='true']",
        "xpath=//div[@contenteditable='true']",
        "xpath=//*[@contenteditable='true']",
        "xpath=//div[contains(@class, 'chat-input-')]",
        "xpath=//*[contains(@placeholder, '输入')]",
        "xpath=//*[contains(@placeholder, '消息')]",
        "xpath=//*[contains(@placeholder, '说点什么')]",
        "xpath=//*[contains(@aria-label, '输入')]",
        "xpath=//*[contains(@aria-label, '消息')]",
    ]

    for selector in chat_input_selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()

            logger.info(
                f"账号 {account_username} 查找输入框，选择器: {selector}，数量: {count}"
            )

            if count == 0:
                continue

            for i in range(count):
                item = locator.nth(i)

                try:
                    if item.is_visible():
                        logger.info(
                            f"账号 {account_username} 找到聊天输入框，好友 {friend_name}，选择器: {selector}，索引: {i}"
                        )
                        return item
                except Exception:
                    continue

        except Exception:
            logger.exception(f"账号 {account_username} 查找输入框选择器失败: {selector}")
            continue

    save_debug_page(page, f"{account_username}_{friend_name}_chat_input_not_found")
    raise RuntimeError(f"账号 {account_username} 未找到好友 {friend_name} 的聊天输入框")


def wait_chat_input_ready(page, account_username, friend_name, timeout=15000):
    """
    Wait until the chat input truly appears.
    """
    selectors = [
        "css=div[class*='chat-input-'][contenteditable='true']",
        "css=div[class*='chat-editor-'] div[contenteditable='true']",
        "css=div[contenteditable='true']",
        "xpath=//div[contains(@class, 'chat-input-')]",
    ]

    end_time = time.time() + timeout / 1000

    while time.time() < end_time:
        close_popups_and_guides(page, account_username)

        for selector in selectors:
            try:
                locator = page.locator(selector)

                if locator.count() > 0:
                    for i in range(locator.count()):
                        item = locator.nth(i)

                        try:
                            if item.is_visible():
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

        time.sleep(0.5)

    save_debug_page(page, f"{account_username}_{friend_name}_chat_input_wait_timeout")
    return False


def js_set_contenteditable_text(locator, message):
    """
    Set contenteditable text and dispatch input/change events.
    """
    locator.evaluate(
        """
        (el, text) => {
            el.focus();

            el.innerHTML = '';
            el.innerText = text;

            const inputEvent = new InputEvent('input', {
                bubbles: true,
                cancelable: true,
                inputType: 'insertText',
                data: text
            });

            el.dispatchEvent(inputEvent);

            const changeEvent = new Event('change', {
                bubbles: true,
                cancelable: true
            });

            el.dispatchEvent(changeEvent);
        }
        """,
        message,
    )


def send_message_to_friend(page, account_username, friend_name, message):
    """
    Send a message to the currently selected friend.
    """

    time.sleep(2)
    close_popups_and_guides(page, account_username)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        logger.warning(
            f"账号 {account_username} 选择好友后 networkidle 等待超时，继续查找输入框"
        )

    close_popups_and_guides(page, account_username)

    if not wait_chat_input_ready(page, account_username, friend_name, timeout=15000):
        raise RuntimeError(
            f"账号 {account_username} 点击好友 {friend_name} 后没有进入聊天详情页，页面上没有聊天输入框"
        )

    chat_input = find_chat_input(page, account_username, friend_name)

    try:
        chat_input.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    try:
        chat_input.click(timeout=10000)
    except Exception:
        logger.warning(f"账号 {account_username} 输入框 click 失败，尝试 JS 聚焦")
        try:
            chat_input.evaluate("(el) => el.focus()")
        except Exception:
            logger.exception(f"账号 {account_username} JS 聚焦输入框失败")

    input_success = False

    try:
        js_set_contenteditable_text(chat_input, message)
        input_success = True
        logger.info(f"账号 {account_username} JS 写入消息成功")
    except Exception:
        logger.warning(f"账号 {account_username} JS 输入失败，改用键盘输入")

    if not input_success:
        try:
            chat_input.click(timeout=5000)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")

            lines = message.split("\n")

            for index, line in enumerate(lines):
                page.keyboard.type(line, delay=20)

                if index != len(lines) - 1:
                    page.keyboard.press("Shift+Enter")

            input_success = True

        except Exception:
            logger.exception(f"账号 {account_username} 键盘输入失败")
            save_debug_page(page, f"{account_username}_{friend_name}_input_failed")
            raise

    logger.debug(
        f"账号 {account_username} 准备发送消息给好友 {friend_name}：\n\t{message}"
    )

    time.sleep(0.8)
    close_popups_and_guides(page, account_username)

    try:
        page.keyboard.press("Enter")
    except Exception:
        logger.exception(f"账号 {account_username} 回车发送失败")
        save_debug_page(page, f"{account_username}_{friend_name}_send_failed")
        raise

    logger.info(f"账号 {account_username} 给好友 {friend_name} 发送消息完成")

    time.sleep(2)


def scroll_and_select_user(page, account_username, targets):
    """
    Scroll and search target friend names.
    """

    friends_tab_selector = 'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]'

    target_selector = (
        'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]'
        '//div[contains(@class, "semi-list-item-body semi-list-item-body-flex-start")]'
    )

    fallback_target_selectors = [
        target_selector,
        'xpath=//div[contains(@class, "semi-list-item-body")]',
        'xpath=//li//div[contains(@class, "semi-list-item")]',
        'xpath=//span[contains(@class, "item-header-name-")]/ancestor::div[contains(@class, "semi-list-item-body")]',
    ]

    scrollable_friends_selectors = [
        'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]/div/div/div[3]/div/div/div/ul/div',
        'xpath=//ul/div',
        'xpath=//div[contains(@class, "semi-list")]/div',
        'xpath=//*[@id="sub-app"]//ul/ancestor::div[contains(@style, "overflow")]',
    ]

    no_more_selector = 'xpath=//div[contains(@class, "no-more-tip-")]'
    loading_selector = 'xpath=//div[contains(@class, "semi-spin")]'

    first_friend_selectors = [
        'xpath=//*[@id="sub-app"]/div/div/div[2]/div[2]/div/div/div[1]/div/div/div/ul/div/div/div[1]/li/div',
        'xpath=//li//div[contains(@class, "semi-list-item")]',
        'xpath=//div[contains(@class, "semi-list-item-body")]',
        'xpath=//span[contains(@class, "item-header-name-")]',
        target_selector,
    ]

    logger.debug(f"账号 {account_username} 开始查找目标好友列表")
    logger.debug(f"账号 {account_username} 目标好友列表: {targets}")

    close_popups_and_guides(page, account_username)

    logger.debug(f"账号 {account_username} 准备切换私信标签页")

    selected_tab = select_message_tab(page, account_username)

    if selected_tab:
        logger.debug(f"账号 {account_username} 已进入私信标签页: {selected_tab}")
    else:
        logger.debug(f"账号 {account_username} 未切换私信标签页，继续使用当前页面")

    time.sleep(2)
    close_popups_and_guides(page, account_username)

    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        logger.warning(f"账号 {account_username} networkidle 等待超时，继续检查页面")

    check_page_status(page, account_username)
    close_popups_and_guides(page, account_username)

    first_friend_locator = get_first_visible_locator(
        page,
        first_friend_selectors,
        timeout=15000,
    )

    if first_friend_locator:
        try:
            first_friend_locator.click()
            logger.debug(f"账号 {account_username} 已激活好友列表")
        except Exception:
            logger.warning(f"账号 {account_username} 找到好友元素但点击失败，继续执行")
    else:
        logger.warning(
            f"账号 {account_username} 未找到好友列表首个元素，尝试直接按目标好友文本点击"
        )

        direct_result = click_target_by_text_directly(page, account_username, targets)

        if direct_result:
            direct_target_name, direct_matched_target = direct_result
            remaining_direct_targets = set(targets)
            remaining_direct_targets.discard(direct_matched_target)
            logger.info(
                f"账号 {account_username} 已通过文本直点选中目标好友: {direct_target_name}"
            )
            yield direct_target_name
            save_found_friends(account_username, {direct_target_name})
            raise_if_targets_missing(account_username, remaining_direct_targets)
            return

        logger.error(f"账号 {account_username} 未找到好友列表首个元素")
        save_debug_page(page, f"{account_username}_friend_list_not_found")
        raise RuntimeError(
            f"账号 {account_username} 未找到好友列表，可能是 Cookie 失效、验证码、安全验证、页面结构变化或好友列表为空"
        )

    time.sleep(config["friendListTimeout"] / 1000)

    found_targets = set()
    remaining_targets = set(targets)

    empty_scroll_count = 0
    max_empty_scrolls = 10

    while True:
        close_popups_and_guides(page, account_username)

        target_elements = []

        for selector in fallback_target_selectors:
            try:
                elements = page.locator(selector).all()

                if elements:
                    target_elements = elements
                    logger.debug(
                        f"账号 {account_username} 使用好友选择器: {selector}，数量: {len(elements)}"
                    )
                    break

            except Exception:
                continue

        prev_found_count = len(found_targets)

        logger.debug(
            f"账号 {account_username} 当前页面好友元素数量: {len(target_elements)}"
        )

        for element in target_elements:
            try:
                span = element.locator(
                    'xpath=.//span[contains(@class, "item-header-name-")]'
                )

                if span.count() == 0:
                    continue

                target_name = span.first.inner_text().strip()

                if not target_name:
                    continue

                logger.info(f"账号 {account_username} 扫描到好友: {target_name}")

                if target_name in found_targets:
                    continue

                if matchMode == "short_id":
                    target_symbol = next(
                        (
                            sid
                            for sid, info in userIDDict.items()
                            if info.get("nickname") == target_name
                        ),
                        None,
                    )
                else:
                    target_symbol = target_name

                matched, matched_target = is_target_match(
                    target_symbol,
                    target_name,
                    targets,
                )

                if matched:
                    clicked = click_friend_element(
                        page,
                        element,
                        account_username,
                        target_name,
                    )

                    if not clicked:
                        logger.warning(
                            f"账号 {account_username} 目标好友 {target_name} 点击失败，继续尝试后续元素"
                        )
                        continue

                    logger.info(
                        f"账号 {account_username} 选中目标好友: {target_name}，匹配目标: {matched_target}"
                    )

                    if matched_target in remaining_targets:
                        remaining_targets.remove(matched_target)

                    found_targets.add(target_name)

                    time.sleep(2)
                    close_popups_and_guides(page, account_username)

                    yield target_name

                    if len(remaining_targets) == 0:
                        logger.info(
                            f"账号 {account_username} 所有目标好友均已找到，停止搜索"
                        )
                        save_found_friends(account_username, found_targets)
                        return

                    break

                found_targets.add(target_name)

            except Exception:
                logger.exception(f"账号 {account_username} 解析好友元素失败")

        else:
            new_found = len(found_targets) > prev_found_count

            if new_found:
                empty_scroll_count = 0
            else:
                empty_scroll_count += 1

            if page.locator(no_more_selector).count() > 0:
                logger.info(f"账号 {account_username} 检测到没有更多了，已到达底部")
                save_found_friends(account_username, found_targets)
                save_debug_page(page, f"{account_username}_reach_bottom")

                if len(remaining_targets) > 0:
                    logger.warning(
                        f"账号 {account_username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
                    )

                break

            if empty_scroll_count >= max_empty_scrolls:
                logger.warning(
                    f"账号 {account_username} 连续 {max_empty_scrolls} 次滚动未发现新好友，判定已到达底部"
                )

                save_found_friends(account_username, found_targets)
                save_debug_page(page, f"{account_username}_empty_scroll_bottom")

                if len(remaining_targets) > 0:
                    logger.warning(
                        f"账号 {account_username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
                    )

                break

            if page.locator(loading_selector).count() > 0:
                logger.debug(f"账号 {account_username} 列表正在加载中")
                time.sleep(1.5)

            scrollable_element = None

            for scroll_selector in scrollable_friends_selectors:
                try:
                    locator = page.locator(scroll_selector)

                    if locator.count() > 0:
                        scrollable_element = locator.first.element_handle(timeout=3000)

                        if scrollable_element:
                            logger.debug(
                                f"账号 {account_username} 使用滚动容器: {scroll_selector}"
                            )
                            break

                except Exception:
                    continue

            if scrollable_element:
                try:
                    scroll_top_before = page.evaluate(
                        "(element) => element.scrollTop",
                        scrollable_element,
                    )

                    page.evaluate(
                        "(element) => element.scrollTop += 800",
                        scrollable_element,
                    )

                    time.sleep(0.5)

                    scroll_top_after = page.evaluate(
                        "(element) => element.scrollTop",
                        scrollable_element,
                    )

                    if scroll_top_before == scroll_top_after:
                        empty_scroll_count += 2
                        logger.debug(
                            f"账号 {account_username} scrollTop 未变化 "
                            f"({scroll_top_before})，可能已到底 "
                            f"({empty_scroll_count}/{max_empty_scrolls})"
                        )
                    else:
                        logger.debug(
                            f"账号 {account_username} 滚动好友列表 "
                            f"(scrollTop: {scroll_top_before} -> {scroll_top_after})"
                        )

                    time.sleep(1.5)

                except Exception:
                    logger.exception(f"账号 {account_username} 滚动好友列表失败")
                    empty_scroll_count += 1

            else:
                logger.error(f"账号 {account_username} 未找到滚动容器")
                save_found_friends(account_username, found_targets)
                save_debug_page(page, f"{account_username}_scroll_container_not_found")
                break

    raise_if_targets_missing(account_username, remaining_targets)


def do_user_task(browser, account_username, cookies, targets):
    context = None
    primary_error = None

    try:
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            screen={"width": 1920, "height": 1080},
        )

        context.set_default_navigation_timeout(config["browserTimeout"])
        context.set_default_timeout(config["browserTimeout"])

        page = context.new_page()

        if matchMode == "short_id":
            page.on("response", handle_response)

        retry_operation(
            "打开抖音创作者中心",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://creator.douyin.com/",
        )

        context.add_cookies(cookies)

        retry_operation(
            "导航到消息页面",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://creator.douyin.com/creator-micro/data/following/chat",
        )

        time.sleep(3)
        close_popups_and_guides(page, account_username)

        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            logger.warning(f"账号 {account_username} 消息页面 networkidle 等待超时")

        close_popups_and_guides(page, account_username)
        check_page_status(page, account_username)

        logger.debug(f"账号 {account_username} 开始发送消息")

        for friend_name in scroll_and_select_user(page, account_username, targets):
            logger.info(f"账号 {account_username} 已选中好友 {friend_name}，准备发送消息")

            message = build_message()
            send_message_to_friend(page, account_username, friend_name, message)

    except BaseException as exc:
        primary_error = exc
        logger.exception(f"账号 {account_username} 执行失败")
        raise

    finally:
        if context:
            try:
                context.close()
            except Exception as exc:
                if primary_error is None:
                    raise

                logger.error(
                    f"账号 {account_username} 失败后的浏览上下文清理也出现异常，"
                    f"保留原始任务错误: {exc}"
                )


def runTasks():
    playwright, browser, chrome_process_guard = get_browser()
    failures = []
    primary_error = None

    try:
        logger.info("开始执行任务")
        logger.debug("当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")

        for user in userData:
            logger.debug(
                f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}"
            )

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            unique_id = user["unique_id"]
            account_username = user.get("username", "未知用户")

            complates[unique_id] = []

            logger.info(f"开始处理账号 {account_username}")

            try:
                do_user_task(browser, account_username, cookies, targets)
                logger.info(f"账号 {account_username} 任务完成")

            except Exception as exc:
                logger.exception(f"账号 {account_username} 任务失败，继续处理下一个账号")

                failures.append((account_username, exc))

        if failures:
            failure_summary = "; ".join(
                f"{username}: {type(exc).__name__}: {exc}"
                for username, exc in failures
            )
            raise RuntimeError(
                f"{len(failures)} 个账号任务失败: {failure_summary}"
            )

    except BaseException as exc:
        primary_error = exc
        raise

    finally:
        cleanup_errors = []

        try:
            tracked_processes = chrome_process_guard.capture_before_close()
            tracked_pids = sorted(
                process.pid
                for process in tracked_processes
            )
            logger.info(f"任务结束前记录本次 Chrome 进程: {tracked_pids}")
        except Exception as exc:
            cleanup_errors.append(f"记录本次 Chrome 进程失败: {exc}")

        try:
            browser.close()
        except Exception as exc:
            cleanup_errors.append(f"关闭浏览器失败: {exc}")

        try:
            terminated_pids = chrome_process_guard.cleanup()

            if terminated_pids:
                logger.info(
                    f"已清理本次运行残留的 Chrome 进程: {terminated_pids}"
                )
            else:
                logger.info("本次运行的 Chrome 进程已全部退出")
        except Exception as exc:
            cleanup_errors.append(f"清理本次 Chrome 进程失败: {exc}")

        try:
            playwright.stop()
        except Exception as exc:
            cleanup_errors.append(f"停止 Playwright 失败: {exc}")

        if cleanup_errors:
            cleanup_summary = "; ".join(cleanup_errors)

            if primary_error is None:
                raise RuntimeError(cleanup_summary)

            logger.error(
                f"任务失败后的资源清理也出现异常，保留原始任务错误: {cleanup_summary}"
            )


# ---------------------------------------------------------------------------
# Override click logic: precise row-left clicking for Douyin group conversations.
# This section intentionally redefines earlier functions.
# ---------------------------------------------------------------------------

def _click_point_and_wait_chat(page, account_username, x, y, target_name, label):
    """
    Click a coordinate and wait briefly for chat pane to open.
    """
    try:
        page.mouse.move(x, y)
        time.sleep(0.2)
        page.mouse.click(x, y)
        time.sleep(1.2)
        close_popups_and_guides(page, account_username)

        if chat_opened(page):
            logger.info(f"账号 {account_username} {label} 点击后打开聊天: {target_name}")
            return True

        page.mouse.dblclick(x, y)
        time.sleep(1.5)
        close_popups_and_guides(page, account_username)

        if chat_opened(page):
            logger.info(f"账号 {account_username} {label} 双击后打开聊天: {target_name}")
            return True

    except Exception:
        pass

    return False


def _click_visible_target_text_precisely(page, account_username, target_name):
    """
    Click the visible target text itself, then click the row-left area around it.

    In the current Douyin UI, clicking the center of a large ancestor/container can
    hit empty space or hover actions. The stable click point is the visible group
    name/avatar area on the left side of the row.
    """
    target_text = str(target_name or "").strip()

    if not target_text:
        return False

    literal = xpath_literal(target_text)

    selectors = [
        f"xpath=//*[@id='sub-app']//*[normalize-space()={literal}]",
        f"xpath=//*[@id='sub-app']//*[contains(normalize-space(), {literal})]",
        f"xpath=//*[normalize-space()={literal}]",
        f"xpath=//*[contains(normalize-space(), {literal})]",
        f"text={target_text}",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()

            logger.info(
                f"账号 {account_username} 精准查找会话文本，目标: {target_text}，选择器: {selector}，数量: {count}"
            )

            for i in range(count):
                item = locator.nth(i)

                try:
                    if not item.is_visible(timeout=1000):
                        continue
                except Exception:
                    continue

                try:
                    item.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass

                text_box = item.bounding_box()

                if not text_box:
                    continue

                text_center_x = text_box["x"] + text_box["width"] / 2
                text_center_y = text_box["y"] + text_box["height"] / 2

                # 1. Click exactly on the visible name text.
                if _click_point_and_wait_chat(
                    page,
                    account_username,
                    text_center_x,
                    text_center_y,
                    target_text,
                    "文本中心",
                ):
                    return True

                # 2. Click slightly inside the name text.
                if _click_point_and_wait_chat(
                    page,
                    account_username,
                    text_box["x"] + 8,
                    text_center_y,
                    target_text,
                    "文本左侧",
                ):
                    return True

                # 3. Click the avatar/name row-left area. This avoids right-side
                # hover actions such as 已读 / 删除 / 复选框.
                row_y = text_center_y
                row_left_points = [
                    (max(260, text_box["x"] - 48), row_y),
                    (max(240, text_box["x"] - 70), row_y),
                    (max(280, text_box["x"] + 20), row_y),
                ]

                for x, y in row_left_points:
                    if _click_point_and_wait_chat(
                        page,
                        account_username,
                        x,
                        y,
                        target_text,
                        "会话左侧区域",
                    ):
                        return True

                # 4. Try small ancestors, but click their left side rather than center.
                ancestors = [
                    item.locator("xpath=ancestor::li[1]"),
                    item.locator("xpath=ancestor::div[contains(@class, 'semi-list-item')][1]"),
                    item.locator("xpath=ancestor::div[contains(@class, 'conversation')][1]"),
                    item.locator("xpath=ancestor::div[contains(@class, 'item')][1]"),
                    item.locator("xpath=ancestor::div[contains(@class, 'list')][1]"),
                ]

                for ancestor in ancestors:
                    try:
                        if ancestor.count() == 0:
                            continue

                        a = ancestor.first

                        if not a.is_visible(timeout=1000):
                            continue

                        a_box = a.bounding_box()

                        if not a_box:
                            continue

                        # Only use reasonable-height row-like boxes.
                        if a_box["height"] > 140:
                            continue

                        x = max(a_box["x"] + 40, min(text_box["x"] + 10, a_box["x"] + 220))
                        y = a_box["y"] + a_box["height"] / 2

                        if _click_point_and_wait_chat(
                            page,
                            account_username,
                            x,
                            y,
                            target_text,
                            "祖先行左侧",
                        ):
                            return True

                    except Exception:
                        continue

                # 5. JS event fallback on the visible text node.
                try:
                    item.evaluate(
                        """
                        (el) => {
                            el.scrollIntoView({ block: 'center', inline: 'nearest' });

                            const eventOptions = {
                                bubbles: true,
                                cancelable: true,
                                view: window
                            };

                            el.dispatchEvent(new MouseEvent('mouseover', eventOptions));
                            el.dispatchEvent(new MouseEvent('mousedown', eventOptions));
                            el.dispatchEvent(new MouseEvent('mouseup', eventOptions));
                            el.dispatchEvent(new MouseEvent('click', eventOptions));

                            const li = el.closest('li');
                            const row =
                                li ||
                                el.closest('[class*="semi-list-item"]') ||
                                el.closest('[class*="conversation"]') ||
                                el.closest('[class*="item"]') ||
                                el;

                            if (row && row !== el) {
                                row.dispatchEvent(new MouseEvent('mouseover', eventOptions));
                                row.dispatchEvent(new MouseEvent('mousedown', eventOptions));
                                row.dispatchEvent(new MouseEvent('mouseup', eventOptions));
                                row.dispatchEvent(new MouseEvent('click', eventOptions));
                            }
                        }
                        """
                    )
                    time.sleep(1.5)
                    close_popups_and_guides(page, account_username)

                    if chat_opened(page):
                        logger.info(f"账号 {account_username} JS 精准点击后打开聊天: {target_text}")
                        return True

                except Exception:
                    pass

        except Exception:
            continue

    return False


def click_friend_element(page, element, account_username, target_name):
    """
    Click a friend/group conversation.

    Redefined: prioritize the visible group name/avatar area instead of clicking
    large ancestor/container centers.
    """
    close_popups_and_guides(page, account_username)

    if _click_visible_target_text_precisely(page, account_username, target_name):
        return True

    # Fallback: use the current element's own text and click its row-left area.
    try:
        text = element.inner_text(timeout=1000).strip()
    except Exception:
        text = target_name

    if text and _click_visible_target_text_precisely(page, account_username, text):
        return True

    logger.error(f"账号 {account_username} 点击好友后没有打开聊天详情: {target_name}")
    save_debug_page(page, f"{account_username}_{target_name}_chat_not_opened")
    return False


def click_target_by_text_directly(page, account_username, targets):
    """
    Directly click target conversation by visible text.

    Redefined: try current tab, then 全部 / 群消息 / 朋友私信 / 陌生人私信.
    """
    close_popups_and_guides(page, account_username)

    tabs_to_try = [None, "全部", "群消息", "朋友私信", "陌生人私信"]

    for tab_name in tabs_to_try:
        if tab_name:
            try:
                literal_tab = xpath_literal(tab_name)
                tab_locator = page.locator(
                    f"xpath=//*[@id='sub-app']//*[normalize-space()={literal_tab}]"
                )

                if tab_locator.count() > 0:
                    for i in range(tab_locator.count()):
                        tab_item = tab_locator.nth(i)

                        try:
                            if not tab_item.is_visible(timeout=1000):
                                continue
                        except Exception:
                            continue

                        tab_item.click(timeout=5000, force=True)
                        time.sleep(2)
                        close_popups_and_guides(page, account_username)
                        logger.info(f"账号 {account_username} 已切换私信标签后准备直点: {tab_name}")
                        break

            except Exception:
                pass

        for target in targets:
            target_text = str(target or "").strip()

            if not target_text:
                continue

            if _click_visible_target_text_precisely(page, account_username, target_text):
                logger.info(f"账号 {account_username} 直接按文本打开聊天: {target_text}")
                return target_text, target_text

    save_debug_page(page, f"{account_username}_direct_text_click_not_opened")
    return None


# ---------------------------------------------------------------------------
# Override send logic: use real input events and verify sending.
# This section intentionally redefines send_message_to_friend.
# ---------------------------------------------------------------------------

def get_editable_text(locator):
    """
    Read text from a contenteditable element.
    """
    try:
        return locator.evaluate("(el) => (el.innerText || el.textContent || '').trim()")
    except Exception:
        return ""


def clear_chat_input(page, chat_input):
    """
    Clear chat input using keyboard selection first, then DOM fallback.
    """
    try:
        chat_input.click(timeout=5000)
        page.keyboard.press("Control+A")
        time.sleep(0.1)
        page.keyboard.press("Backspace")
        time.sleep(0.2)
    except Exception:
        pass

    try:
        chat_input.evaluate(
            """
            (el) => {
                el.focus();
                el.innerHTML = '';
                el.innerText = '';
                el.textContent = '';
                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    cancelable: true,
                    inputType: 'deleteContentBackward',
                    data: null
                }));
            }
            """
        )
    except Exception:
        pass


def type_message_with_real_events(page, chat_input, message):
    """
    Put message into the contenteditable editor using methods that React can observe.

    JS innerText alone may change the DOM but not Douyin/React internal state,
    leaving the send button disabled. Therefore this tries locator.fill(),
    keyboard.insert_text(), and keyboard.type().
    """
    message = str(message or "")

    clear_chat_input(page, chat_input)

    # Method 1: Playwright fill supports contenteditable and fires input events.
    try:
        chat_input.fill(message, timeout=5000)
        time.sleep(0.8)

        if get_editable_text(chat_input):
            return "fill"

    except Exception:
        pass

    clear_chat_input(page, chat_input)

    # Method 2: insert_text inserts text as real input without relying on keyboard layout.
    try:
        chat_input.click(timeout=5000)
        page.keyboard.insert_text(message)
        time.sleep(0.8)

        if get_editable_text(chat_input):
            return "insert_text"

    except Exception:
        pass

    clear_chat_input(page, chat_input)

    # Method 3: keyboard typing fallback, preserving newlines with Shift+Enter.
    try:
        chat_input.click(timeout=5000)

        lines = message.split("\n")

        for index, line in enumerate(lines):
            page.keyboard.type(line, delay=30)

            if index != len(lines) - 1:
                page.keyboard.press("Shift+Enter")

        time.sleep(0.8)

        if get_editable_text(chat_input):
            return "keyboard_type"

    except Exception:
        pass

    # Method 4: JS fallback, but only as last resort.
    try:
        js_set_contenteditable_text(chat_input, message)
        time.sleep(0.8)

        if get_editable_text(chat_input):
            return "js_fallback"

    except Exception:
        pass

    return None


def find_send_button(page):
    """
    Find a visible send button.
    """
    selectors = [
        "xpath=//button[normalize-space()='发送']",
        "xpath=//*[self::button or self::div or self::span][normalize-space()='发送']",
        "xpath=//*[contains(@class, 'send') and contains(normalize-space(), '发送')]",
        "css=button:has-text('发送')",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()

            for i in range(count):
                item = locator.nth(i)

                try:
                    if item.is_visible(timeout=500):
                        return item
                except Exception:
                    continue

        except Exception:
            continue

    return None


def is_send_button_enabled(button):
    """
    Check whether the send button looks enabled.
    """
    if button is None:
        return False

    try:
        return button.evaluate(
            """
            (el) => {
                const style = window.getComputedStyle(el);
                const className = String(el.className || '');

                if (el.disabled) return false;
                if (el.getAttribute('aria-disabled') === 'true') return false;
                if (className.includes('disabled')) return false;
                if (style.pointerEvents === 'none') return false;
                if (Number(style.opacity || '1') < 0.6) return false;

                return true;
            }
            """
        )
    except Exception:
        return False


def click_send_or_press_enter(page, account_username, friend_name):
    """
    Send by clicking enabled send button first; fall back to Enter.
    """
    button = None

    for _ in range(20):
        button = find_send_button(page)

        if button and is_send_button_enabled(button):
            try:
                button.click(timeout=5000, force=True)
                logger.info(f"账号 {account_username} 已点击发送按钮: {friend_name}")
                return "button"
            except Exception:
                pass

        time.sleep(0.25)

    try:
        page.keyboard.press("Enter")
        logger.info(f"账号 {account_username} 已按 Enter 发送: {friend_name}")
        return "enter"
    except Exception:
        logger.exception(f"账号 {account_username} Enter 发送失败")
        return None


def wait_input_cleared(chat_input, timeout=8000):
    """
    After a successful send, Douyin usually clears the editor.
    """
    end_time = time.time() + timeout / 1000

    while time.time() < end_time:
        text = get_editable_text(chat_input)

        if not text:
            return True

        time.sleep(0.4)

    return False


def send_message_to_friend(page, account_username, friend_name, message):
    """
    Send a message to the currently selected friend/group.

    This override avoids the old JS-only write path. JS innerText can make the
    log say "success" while Douyin's React state still thinks the input is empty.
    """
    message = str(message or "").strip()

    if not message:
        logger.warning(f"账号 {account_username} 消息为空，跳过发送")
        return

    time.sleep(2)
    close_popups_and_guides(page, account_username)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        logger.warning(
            f"账号 {account_username} 选择好友后 networkidle 等待超时，继续查找输入框"
        )

    close_popups_and_guides(page, account_username)

    if not wait_chat_input_ready(page, account_username, friend_name, timeout=15000):
        raise RuntimeError(
            f"账号 {account_username} 点击好友 {friend_name} 后没有进入聊天详情页，页面上没有聊天输入框"
        )

    chat_input = find_chat_input(page, account_username, friend_name)

    try:
        chat_input.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    try:
        chat_input.click(timeout=10000)
    except Exception:
        logger.warning(f"账号 {account_username} 输入框 click 失败，尝试 JS 聚焦")
        try:
            chat_input.evaluate("(el) => el.focus()")
        except Exception:
            logger.exception(f"账号 {account_username} JS 聚焦输入框失败")

    input_method = type_message_with_real_events(page, chat_input, message)

    current_text = get_editable_text(chat_input)

    if not input_method or not current_text:
        logger.error(f"账号 {account_username} 输入消息失败，输入框仍为空")
        save_debug_page(page, f"{account_username}_{friend_name}_input_empty_after_type")
        raise RuntimeError(f"账号 {account_username} 输入消息失败")

    logger.info(
        f"账号 {account_username} 已输入消息，方法: {input_method}，当前输入框内容长度: {len(current_text)}"
    )

    send_method = click_send_or_press_enter(page, account_username, friend_name)

    if not send_method:
        save_debug_page(page, f"{account_username}_{friend_name}_send_action_failed")
        raise RuntimeError(f"账号 {account_username} 发送动作失败")

    if wait_input_cleared(chat_input, timeout=8000):
        logger.info(
            f"账号 {account_username} 给好友 {friend_name} 发送消息完成，发送方式: {send_method}"
        )
        return

    # If the input is not cleared, sending likely failed even if button/enter ran.
    logger.error(
        f"账号 {account_username} 发送后输入框未清空，可能没有真正发出去"
    )
    save_debug_page(page, f"{account_username}_{friend_name}_send_not_confirmed")
    raise RuntimeError(
        f"账号 {account_username} 发送后输入框未清空，未确认发送成功"
    )


# ---------------------------------------------------------------------------
# Override send-button logic: click the real chat button and verify immediately.
# This section intentionally redefines find_send_button, click_send_or_press_enter,
# and send_message_to_friend.
# ---------------------------------------------------------------------------

def find_send_button(page):
    """
    Find the real visible send button in the chat footer.

    Actual DOM from debug HTML:
    <button class="semi-button semi-button-primary chat-btn" type="button">
        <span class="semi-button-content">发送</span>
    </button>
    """
    selectors = [
        "css=div[class*='chat-footer-'] button.chat-btn",
        "css=button.chat-btn",
        "css=div[class*='chat-footer-'] button.semi-button-primary",
        "xpath=//div[contains(@class, 'chat-footer-')]//button[contains(@class, 'chat-btn')]",
        "xpath=//div[contains(@class, 'chat-footer-')]//button[.//span[normalize-space()='发送'] or normalize-space()='发送']",
        "xpath=//button[contains(@class, 'semi-button-primary') and (.//span[normalize-space()='发送'] or normalize-space()='发送')]",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()

            logger.info(f"查找发送按钮，选择器: {selector}，数量: {count}")

            for i in range(count):
                item = locator.nth(i)

                try:
                    if item.is_visible(timeout=500):
                        return item
                except Exception:
                    continue

        except Exception:
            continue

    return None


def is_send_button_enabled(button):
    """
    Check whether the send button is enabled.
    """
    if button is None:
        return False

    try:
        return button.evaluate(
            """
            (el) => {
                const button = el.closest('button') || el;
                const style = window.getComputedStyle(button);
                const className = String(button.className || '');

                if (button.disabled) return false;
                if (button.getAttribute('disabled') !== null) return false;
                if (button.getAttribute('aria-disabled') === 'true') return false;
                if (className.includes('disabled')) return false;
                if (style.pointerEvents === 'none') return false;
                if (Number(style.opacity || '1') < 0.45) return false;

                return true;
            }
            """
        )
    except Exception:
        return False


def get_button_debug_info(button):
    """
    Return compact debug info for the send button.
    """
    if button is None:
        return "button=None"

    try:
        return button.evaluate(
            """
            (el) => {
                const button = el.closest('button') || el;
                const style = window.getComputedStyle(button);
                const rect = button.getBoundingClientRect();
                const hit = document.elementFromPoint(
                    rect.left + rect.width / 2,
                    rect.top + rect.height / 2
                );

                return JSON.stringify({
                    tag: button.tagName,
                    className: String(button.className || ''),
                    text: (button.innerText || '').trim(),
                    disabled: !!button.disabled,
                    ariaDisabled: button.getAttribute('aria-disabled'),
                    pointerEvents: style.pointerEvents,
                    opacity: style.opacity,
                    rect: {
                        x: rect.x,
                        y: rect.y,
                        width: rect.width,
                        height: rect.height
                    },
                    hitInsideButton: !!hit && button.contains(hit),
                    hitTarget: hit ? {
                        tag: hit.tagName,
                        className: String(hit.className || ''),
                        text: (hit.innerText || '').trim().slice(0, 40)
                    } : null
                });
            }
            """
        )
    except Exception as e:
        return f"button_debug_failed: {e}"


def dispatch_button_events(button):
    """
    Dispatch pointer/mouse events on the real button element.
    """
    button.evaluate(
        """
        (el) => {
            const button = el.closest('button') || el;
            button.scrollIntoView({ block: 'center', inline: 'center' });

            const rect = button.getBoundingClientRect();
            const x = rect.left + rect.width / 2;
            const y = rect.top + rect.height / 2;

            const base = {
                bubbles: true,
                cancelable: true,
                composed: true,
                view: window,
                clientX: x,
                clientY: y,
                screenX: x,
                screenY: y,
                button: 0,
                buttons: 1
            };

            button.dispatchEvent(new PointerEvent('pointerover', base));
            button.dispatchEvent(new MouseEvent('mouseover', base));
            button.dispatchEvent(new PointerEvent('pointermove', base));
            button.dispatchEvent(new MouseEvent('mousemove', base));
            button.dispatchEvent(new PointerEvent('pointerdown', base));
            button.dispatchEvent(new MouseEvent('mousedown', base));
            button.dispatchEvent(new PointerEvent('pointerup', { ...base, buttons: 0 }));
            button.dispatchEvent(new MouseEvent('mouseup', { ...base, buttons: 0 }));
            button.dispatchEvent(new MouseEvent('click', { ...base, buttons: 0 }));
        }
        """
    )


class SendActionNotStarted(RuntimeError):
    """Raised while it is still safe to abort without confirming a send."""


def ensure_expected_chat_input(chat_input, expected_message):
    """Prevent a send when React changed or cleared the editor."""
    if chat_input is None:
        raise SendActionNotStarted("聊天输入框不可用，禁止执行发送动作")

    actual_message = normalize_chat_text(get_editable_text(chat_input))
    expected_message = normalize_chat_text(expected_message)

    if not expected_message or actual_message != expected_message:
        raise SendActionNotStarted(
            "发送前输入框内容与预期消息不一致，禁止执行发送动作"
        )


def plan_single_send(button, chat_input, expected_message):
    """
    Select exactly one real send action without triggering a send.

    Playwright's trial click runs the normal actionability checks, including
    whether the button can receive pointer events, but skips the click itself.
    """
    ensure_expected_chat_input(chat_input, expected_message)

    try:
        button.click(trial=True, timeout=3000)
        return "locator_button", "发送按钮已通过可操作性检查"
    except PlaywrightTimeoutError as exc:
        return (
            "enter",
            f"发送按钮未通过可操作性检查: {type(exc).__name__}",
        )
    except Exception as exc:
        raise SendActionNotStarted(
            f"发送按钮可操作性检查异常: {type(exc).__name__}"
        ) from exc


def dispatch_single_send(button, chat_input, expected_message, method):
    """Execute one, and only one, potentially sending action."""
    ensure_expected_chat_input(chat_input, expected_message)

    if method == "locator_button":
        button.click(timeout=5000)
        return

    if method == "enter":
        chat_input.press("Enter", timeout=5000)
        return

    raise ValueError(f"未知发送方式: {method}")


def click_send_or_press_enter(
    page,
    account_username,
    friend_name,
    chat_input=None,
    expected_message=None,
):
    """
    Plan and execute a single send action.

    Once a real action starts, never try another method. A Playwright action
    can throw after dispatching its event, so falling back at that point can
    duplicate a message.
    """

    button = None

    for _ in range(30):
        button = find_send_button(page)

        if button and is_send_button_enabled(button):
            break

        time.sleep(0.25)

    if button is None or not is_send_button_enabled(button):
        logger.error(f"账号 {account_username} 未找到可用发送按钮")
        return None

    try:
        method, reason = plan_single_send(
            button,
            chat_input,
            expected_message,
        )
    except SendActionNotStarted as exc:
        logger.error(f"账号 {account_username} {exc}")
        return None

    logger.info(f"账号 {account_username} 发送按钮状态: {get_button_debug_info(button)}")
    logger.info(
        f"账号 {account_username} 发送动作规划: {method}，原因: {reason}"
    )

    try:
        dispatch_single_send(button, chat_input, expected_message, method)
        logger.info(
            f"账号 {account_username} 已执行唯一发送动作 {method}: {friend_name}"
        )
        return method
    except SendActionNotStarted as exc:
        logger.error(f"账号 {account_username} {exc}")
        return None
    except Exception:
        logger.exception(
            f"账号 {account_username} 发送动作 {method} 抛出异常，"
            "动作结果不确定，不再尝试其他发送方式"
        )
        return f"{method}_ambiguous"


def wait_input_cleared(chat_input, timeout=12000):
    """
    After a successful send, Douyin usually clears the editor.
    """
    end_time = time.time() + timeout / 1000

    while time.time() < end_time:
        text = get_editable_text(chat_input)

        if not text:
            return True

        time.sleep(0.4)

    return False


OUTGOING_MESSAGE_SELECTOR = (
    "css=#sub-app div[class*='box-content-'] "
    "div[class*='box-item-'][class*='is-me-']"
)
OUTGOING_MESSAGE_TEXT_SELECTOR = "css=[class*='text-item-message-']"
OUTGOING_MESSAGE_STATUS_SELECTOR = "css=[class*='box-item-message-status-']"
OUTGOING_MESSAGE_PENDING_SELECTOR = "css=[class*='sending-']"
CHAT_REJECTION_TIP_SELECTOR = (
    "css=#sub-app div[class*='box-content-'] "
    "div[class*='box-item-'][class*='tip-']:visible"
)
SEND_BASELINE_ATTRIBUTE = "data-dysf-send-baseline"
CONFER_PERMISSION_CHECK_PATH = "/aweme/v1/creator/confer/permission/check/"
SEND_DIAGNOSTIC_WINDOW_KEY = "__dysfSendDiagnostic"
SEND_BUTTON_EVENT_TYPES = ("pointerdown", "pointerup", "click")
SEND_DIAGNOSTIC_STATUS_MSG_LIMIT = 160


class MessageSendNotConfirmed(RuntimeError):
    """Raised when the page never exposes a successful outgoing message state."""


class SendAttemptDiagnosticMonitor:
    """Observe one send attempt without issuing requests or input actions."""

    def __init__(self, page):
        self.page = page
        self.permission_state = "not_observed"
        self.permission_request_count = 0
        self.http_status = None
        self.status_code = None
        self.check_result = None
        self.status_msg = ""
        self.button_events = {
            event_type: {"count": 0, "is_trusted": False}
            for event_type in SEND_BUTTON_EVENT_TYPES
        }
        self._permission_events = deque()
        self._permission_requests = {}
        self._latest_permission_request_key = None
        self._listeners = []
        self._started = False

    @staticmethod
    def _is_permission_check(url):
        return CONFER_PERMISSION_CHECK_PATH in str(url or "")

    @staticmethod
    def _truncate_status_msg(value):
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        return value[:SEND_DIAGNOSTIC_STATUS_MSG_LIMIT]

    @staticmethod
    def _is_zero(value):
        return value in (0, "0")

    @staticmethod
    def _is_allowed(value):
        return value in (1, "1", True)

    @staticmethod
    def _is_denied(value):
        return value is False or value in (0, "0")

    def _attach_listener(self, event_name, handler):
        try:
            self.page.on(event_name, handler)
            self._listeners.append((event_name, handler))
        except Exception as exc:
            logger.warning(
                f"发送诊断监听器安装失败: {event_name}, {type(exc).__name__}"
            )

    def start(self):
        if self._started:
            return self

        self._started = True
        self._attach_listener("request", self._on_request)
        self._attach_listener("response", self._on_response)
        self._attach_listener("requestfailed", self._on_request_failed)
        self._attach_listener("requestfinished", self._on_request_finished)

        try:
            self.page.evaluate(
                """
                (key) => {
                    const previous = window[key];

                    if (previous && typeof previous.cleanup === 'function') {
                        previous.cleanup();
                    }

                    const events = {
                        pointerdown: { count: 0, isTrusted: false },
                        pointerup: { count: 0, isTrusted: false },
                        click: { count: 0, isTrusted: false }
                    };
                    const eventTypes = Object.keys(events);
                    const handler = (event) => {
                        const target = event.target;
                        const button = target && target.closest
                            ? target.closest('button.chat-btn')
                            : null;

                        if (!button || !events[event.type]) return;

                        events[event.type].count += 1;
                        events[event.type].isTrusted =
                            events[event.type].isTrusted || event.isTrusted === true;
                    };

                    eventTypes.forEach((type) => {
                        document.addEventListener(type, handler, true);
                    });

                    window[key] = {
                        events,
                        cleanup: () => {
                            eventTypes.forEach((type) => {
                                document.removeEventListener(type, handler, true);
                            });
                        }
                    };
                }
                """,
                SEND_DIAGNOSTIC_WINDOW_KEY,
            )
        except Exception as exc:
            logger.warning(
                f"发送按钮事件诊断安装失败: {type(exc).__name__}"
            )

        return self

    def _on_request(self, request):
        if not self._is_permission_check(getattr(request, "url", "")):
            return

        self._permission_events.append(("request", request))

    def _on_response(self, response):
        if not self._is_permission_check(getattr(response, "url", "")):
            return

        self._permission_events.append(("response", response))

    def _on_request_finished(self, request):
        if not self._is_permission_check(getattr(request, "url", "")):
            return

        self._permission_events.append(("request_finished", request))

    def _apply_response(self, response):
        http_status = None

        try:
            http_status = response.status
        except Exception:
            pass

        try:
            payload = response.json()
        except Exception:
            payload = {}

        if not isinstance(payload, dict):
            payload = {}

        status_code = payload.get("status_code")
        check_result = payload.get("check_result")
        status_msg = self._truncate_status_msg(payload.get("status_msg"))

        http_ok = (
            isinstance(http_status, int)
            and 200 <= http_status < 400
        )

        if not http_ok or not self._is_zero(status_code):
            permission_state = "api_error"
        elif self._is_allowed(check_result):
            permission_state = "allowed"
        elif self._is_denied(check_result):
            permission_state = "permission_denied"
        else:
            permission_state = "api_error"

        # Commit one complete response atomically. response.json() yields to
        # Playwright's dispatcher, so callbacks may queue later events while it
        # waits for the body.
        self.http_status = http_status
        self.status_code = status_code
        self.check_result = check_result
        self.status_msg = status_msg
        self.permission_state = permission_state

        logger.info(
            "发送诊断: 代运营权限响应 "
            f"http_status={self.http_status}, "
            f"status_code={self.status_code}, "
            f"check_result={self.check_result}, "
            f"status_msg={self.status_msg!r}"
        )

    def _on_request_failed(self, request):
        if not self._is_permission_check(getattr(request, "url", "")):
            return

        self._permission_events.append(("request_failed", request))

    def refresh(self):
        """Apply queued Playwright events serially outside event callbacks."""
        while self._permission_events:
            event_type, payload = self._permission_events.popleft()

            if event_type == "request":
                request_key = id(payload)

                if request_key in self._permission_requests:
                    continue

                self.permission_request_count += 1
                self._permission_requests[request_key] = {
                    "request": payload,
                    "response": None,
                    "sequence": self.permission_request_count,
                }
                self._latest_permission_request_key = request_key
                self.permission_state = "requested_pending"
                self.http_status = None
                self.status_code = None
                self.check_result = None
                self.status_msg = ""
                logger.info("发送诊断: 已观察到代运营权限校验请求")
                continue

            if event_type == "response":
                try:
                    request = payload.request
                except Exception:
                    continue

                request_key = id(request)
                request_state = self._permission_requests.get(request_key)

                if request_state is None:
                    continue

                request_state["response"] = payload

                if request_key == self._latest_permission_request_key:
                    try:
                        self.http_status = payload.status
                    except Exception:
                        self.http_status = None

                    self.permission_state = "response_pending"

                continue

            request_key = id(payload)
            request_state = self._permission_requests.get(request_key)

            if request_state is None:
                continue

            if request_key != self._latest_permission_request_key:
                continue

            if event_type == "request_finished":
                response = request_state.get("response")

                if response is not None:
                    self._apply_response(response)

                continue

            self.permission_state = "request_failed"
            self.http_status = None
            self.status_code = None
            self.check_result = None
            self.status_msg = ""
            logger.warning("发送诊断: 代运营权限校验请求失败")

    def collect_button_events(self):
        try:
            snapshot = self.page.evaluate(
                """
                (key) => {
                    const state = window[key];

                    if (!state || !state.events) return null;

                    const snapshot = {};

                    Object.entries(state.events).forEach(([type, value]) => {
                        snapshot[type] = {
                            count: Number(value.count || 0),
                            isTrusted: value.isTrusted === true
                        };
                    });

                    return snapshot;
                }
                """,
                SEND_DIAGNOSTIC_WINDOW_KEY,
            )
        except Exception:
            return self.button_events

        if not isinstance(snapshot, dict):
            return self.button_events

        for event_type in SEND_BUTTON_EVENT_TYPES:
            value = snapshot.get(event_type)

            if not isinstance(value, dict):
                continue

            try:
                count = max(0, int(value.get("count", 0)))
            except (TypeError, ValueError):
                count = 0

            current = self.button_events[event_type]
            current["count"] = max(current["count"], count)
            current["is_trusted"] = bool(
                current["is_trusted"] or value.get("isTrusted") is True
            )

        return self.button_events

    def failure_detail(self):
        detail_parts = []

        if self.http_status is not None:
            detail_parts.append(f"HTTP {self.http_status}")

        if self.status_code is not None:
            detail_parts.append(f"status_code={self.status_code}")

        if self.check_result is not None:
            detail_parts.append(f"check_result={self.check_result}")

        if self.status_msg:
            detail_parts.append(f"status_msg={self.status_msg}")

        detail = ", ".join(detail_parts)

        if self.permission_state == "permission_denied":
            prefix = "代运营账号没有私信权限"
        elif self.permission_state == "api_error":
            prefix = "代运营权限校验接口返回异常"
        elif self.permission_state == "request_failed":
            prefix = "代运营权限校验请求失败"
        else:
            return ""

        return f"{prefix}: {detail}" if detail else prefix

    def raise_if_failed(self):
        self.refresh()
        detail = self.failure_detail()

        if detail:
            raise MessageSendNotConfirmed(detail)

    def summary(self):
        self.refresh()
        fields = [f"permission={self.permission_state}"]

        if self.http_status is not None:
            fields.append(f"http_status={self.http_status}")

        if self.status_code is not None:
            fields.append(f"status_code={self.status_code}")

        if self.check_result is not None:
            fields.append(f"check_result={self.check_result}")

        if self.status_msg:
            fields.append(f"status_msg={self.status_msg}")

        event_summary = ",".join(
            f"{event_type}:{details['count']}/trusted={details['is_trusted']}"
            for event_type, details in self.button_events.items()
        )
        fields.append(f"button_events={event_summary}")
        return "; ".join(fields)

    def stop(self):
        if not self._started:
            return

        self.refresh()
        self.collect_button_events()

        try:
            self.page.evaluate(
                """
                (key) => {
                    const state = window[key];

                    if (!state) return;
                    if (typeof state.cleanup === 'function') state.cleanup();
                    delete window[key];
                }
                """,
                SEND_DIAGNOSTIC_WINDOW_KEY,
            )
        except Exception:
            pass

        for event_name, handler in reversed(self._listeners):
            try:
                self.page.remove_listener(event_name, handler)
            except Exception:
                pass

        # Playwright can dispatch late response/finished events while the
        # evaluate calls above yield. Remove listeners first, then drain the
        # events already delivered before clearing request associations.
        self.refresh()
        summary = self.summary()
        self._listeners.clear()
        self._permission_requests.clear()
        self._latest_permission_request_key = None
        self._started = False
        logger.info(f"发送诊断汇总: {summary}")


def normalize_chat_text(value):
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def normalize_message_for_matching(value):
    text = normalize_chat_text(value)
    text = re.sub(r"\[[^\[\]\r\n]{1,32}\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def message_text_matches(rendered_text, expected_text):
    rendered = normalize_chat_text(rendered_text)
    expected = normalize_chat_text(expected_text)

    if rendered == expected:
        return True

    simplified_expected = normalize_message_for_matching(expected)

    return bool(
        simplified_expected
        and normalize_message_for_matching(rendered) == simplified_expected
    )


def visible_locator_count(locator):
    count = locator.count()
    visible = 0

    for index in range(count):
        if locator.nth(index).is_visible(timeout=200):
            visible += 1

    return visible


def outgoing_message_text(bubble):
    try:
        text_nodes = bubble.locator(OUTGOING_MESSAGE_TEXT_SELECTOR)
        count = text_nodes.count()
    except Exception:
        return ""

    for index in range(count):
        node = text_nodes.nth(index)

        try:
            if node.is_visible(timeout=200):
                return normalize_chat_text(node.inner_text(timeout=500))
        except Exception:
            continue

    return ""


def capture_message_send_snapshot(page, message):
    expected_text = normalize_chat_text(message)
    bubbles = page.locator(OUTGOING_MESSAGE_SELECTOR)
    bubble_count = bubbles.count()
    matching_count = 0
    marker = f"dysf-{time.time_ns()}"

    for index in range(bubble_count):
        bubble = bubbles.nth(index)

        if message_text_matches(outgoing_message_text(bubble), expected_text):
            matching_count += 1

        bubble.evaluate(
            "(el, marker) => el.setAttribute('data-dysf-send-baseline', marker)",
            marker,
        )

    rejection_tips = page.locator(CHAT_REJECTION_TIP_SELECTOR)

    return {
        "bubble_count": bubble_count,
        "matching_count": matching_count,
        "rejection_tip_count": visible_locator_count(rejection_tips),
        "marker": marker,
    }


def inspect_new_outgoing_message(page, snapshot, message):
    expected_text = normalize_chat_text(message)
    bubbles = page.locator(OUTGOING_MESSAGE_SELECTOR)
    bubble_count = bubbles.count()
    matching_bubbles = []
    unmarked_matching_bubbles = []
    marker = snapshot["marker"]

    for index in range(bubble_count):
        bubble = bubbles.nth(index)

        if message_text_matches(outgoing_message_text(bubble), expected_text):
            matching_bubbles.append(bubble)

            if bubble.get_attribute(SEND_BASELINE_ATTRIBUTE) != marker:
                unmarked_matching_bubbles.append(bubble)

    if (
        len(matching_bubbles) <= snapshot["matching_count"]
        or not unmarked_matching_bubbles
    ):
        return {"state": "missing", "detail": "未检测到新增的本人消息气泡"}

    candidate = unmarked_matching_bubbles[-1]

    adjacent_tip = candidate.locator(
        "xpath=following-sibling::*[1][contains(@class, 'box-item-') "
        "and contains(@class, 'tip-')]"
    )

    if visible_locator_count(adjacent_tip):
        try:
            detail = normalize_chat_text(adjacent_tip.nth(0).inner_text(timeout=500))
        except Exception:
            detail = "消息旁出现审核/拒绝提示"

        return {"state": "rejected", "detail": detail or "消息被审核/拒绝"}

    rejection_tip_count = visible_locator_count(
        page.locator(CHAT_REJECTION_TIP_SELECTOR)
    )

    if rejection_tip_count > snapshot["rejection_tip_count"]:
        return {"state": "rejected", "detail": "聊天区域出现新的审核/拒绝提示"}

    status = candidate.locator(OUTGOING_MESSAGE_STATUS_SELECTOR)

    if visible_locator_count(status):
        pending = candidate.locator(OUTGOING_MESSAGE_PENDING_SELECTOR)

        if visible_locator_count(pending):
            return {"state": "pending", "detail": "消息仍在发送中"}

        return {"state": "failed", "detail": "消息气泡显示发送失败状态"}

    return {"state": "success", "detail": "消息气泡已进入成功状态"}


def wait_for_message_send_confirmation(
    page,
    snapshot,
    message,
    timeout=25000,
    poll_interval=0.25,
    stable_seconds=3.0,
    diagnostic_monitor=None,
):
    deadline = time.monotonic() + timeout / 1000
    success_since = None
    last_state = {"state": "missing", "detail": "未检测到新增的本人消息气泡"}

    while time.monotonic() < deadline:
        if diagnostic_monitor is not None:
            diagnostic_monitor.raise_if_failed()

        try:
            state = inspect_new_outgoing_message(page, snapshot, message)
        except Exception as exc:
            state = {
                "state": "unknown",
                "detail": f"读取消息状态时页面发生变化: {exc}",
            }

        last_state = state

        if diagnostic_monitor is not None:
            diagnostic_monitor.raise_if_failed()

        if state["state"] in ("failed", "rejected"):
            raise MessageSendNotConfirmed(state["detail"])

        if state["state"] == "success":
            now = time.monotonic()

            if success_since is None:
                success_since = now

            if now - success_since >= stable_seconds:
                return state
        else:
            success_since = None

        time.sleep(poll_interval)

    diagnostic_summary = ""

    if diagnostic_monitor is not None:
        diagnostic_monitor.raise_if_failed()
        diagnostic_monitor.collect_button_events()
        diagnostic_summary = f"; 发送诊断: {diagnostic_monitor.summary()}"

    raise MessageSendNotConfirmed(
        f"发送结果确认超时: {last_state['detail']}{diagnostic_summary}"
    )


def send_message_to_friend(page, account_username, friend_name, message):
    """
    Send a message to the currently selected friend/group.

    This version verifies the real send button, clicks the real chat-btn, and
    only reports success after the new outgoing bubble reaches a stable success
    state without a failure status or moderation/rejection tip.
    """

    message = str(message or "").strip()

    if not message:
        logger.warning(f"账号 {account_username} 消息为空，跳过发送")
        return

    time.sleep(2)
    close_popups_and_guides(page, account_username)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        logger.warning(
            f"账号 {account_username} 选择好友后 networkidle 等待超时，继续查找输入框"
        )

    close_popups_and_guides(page, account_username)

    if not wait_chat_input_ready(page, account_username, friend_name, timeout=15000):
        raise RuntimeError(
            f"账号 {account_username} 点击好友 {friend_name} 后没有进入聊天详情页，页面上没有聊天输入框"
        )

    chat_input = find_chat_input(page, account_username, friend_name)

    try:
        chat_input.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    try:
        chat_input.click(timeout=10000)
    except Exception:
        logger.warning(f"账号 {account_username} 输入框 click 失败，尝试 JS 聚焦")
        try:
            chat_input.evaluate("(el) => el.focus()")
        except Exception:
            logger.exception(f"账号 {account_username} JS 聚焦输入框失败")

    input_method = type_message_with_real_events(page, chat_input, message)
    current_text = get_editable_text(chat_input)

    if not input_method or not current_text:
        logger.error(f"账号 {account_username} 输入消息失败，输入框仍为空")
        save_debug_page(page, f"{account_username}_{friend_name}_input_empty_after_type")
        raise RuntimeError(f"账号 {account_username} 输入消息失败")

    logger.info(
        f"账号 {account_username} 已输入消息，方法: {input_method}，当前输入框内容长度: {len(current_text)}"
    )

    # Give React a short moment to enable the button after input.
    time.sleep(1.0)
    send_snapshot = capture_message_send_snapshot(page, message)
    diagnostic_monitor = SendAttemptDiagnosticMonitor(page).start()

    try:
        send_method = click_send_or_press_enter(
            page,
            account_username,
            friend_name,
            chat_input=chat_input,
            expected_message=message,
        )

        diagnostic_monitor.collect_button_events()

        if not send_method:
            save_debug_page(page, f"{account_username}_{friend_name}_send_action_failed")
            raise RuntimeError(f"账号 {account_username} 发送动作失败")

        try:
            wait_for_message_send_confirmation(
                page,
                send_snapshot,
                message,
                diagnostic_monitor=diagnostic_monitor,
            )
        except MessageSendNotConfirmed as exc:
            logger.error(
                f"账号 {account_username} 给好友 {friend_name} 的消息未确认发送成功: {exc}"
            )
            save_debug_page(page, f"{account_username}_{friend_name}_send_not_confirmed")
            raise RuntimeError(
                f"账号 {account_username} 给好友 {friend_name} 的消息未确认发送成功: {exc}"
            ) from exc
    finally:
        diagnostic_monitor.stop()

    logger.info(
        f"账号 {account_username} 给好友 {friend_name} 发送消息已确认，发送方式: {send_method}"
    )
