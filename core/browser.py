import os
import sys
import shutil
import traceback
from playwright.sync_api import sync_playwright
from utils.config import DEBUG, get_environment, Environment


def find_system_chrome():
    """
    查找系统已经安装好的 Chrome / Chromium
    """

    candidates = [
        os.environ.get("CHROME_PATH"),

        # Google Chrome 常见路径
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",

        # Chromium 常见路径
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]

    for path in candidates:
        if path and os.path.exists(path):
            return path

    return None


def should_run_headless():
    """
    判断是否使用无头模式。

    服务器通常没有 DISPLAY，必须 headless=True。
    本地有图形界面并且 DEBUG=True 时，才允许 headless=False。
    """

    # 如果手动指定了环境变量，优先使用环境变量
    # HEADLESS=false 表示有界面模式
    # HEADLESS=true 表示无头模式
    env_headless = os.environ.get("HEADLESS")
    if env_headless is not None:
        return env_headless.lower() not in ["0", "false", "no"]

    # Linux 服务器没有 DISPLAY，必须使用 headless
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return True

    env = get_environment()

    # 只有本地调试并且存在图形界面时，才关闭 headless
    if env == Environment.LOCAL and DEBUG:
        return False

    return True


def get_browser():
    """
    启动浏览器实例

    :return: playwright, browser
    """

    try:
        # 不使用 Playwright 自己下载的 Chromium
        # 直接使用系统已安装的 Chrome
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)

        playwright = sync_playwright().start()

        chrome_path = find_system_chrome()
        headless = should_run_headless()

        launch_args = [
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        # root 用户运行 Chrome 时通常必须加 --no-sandbox
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            launch_args.append("--no-sandbox")

        if chrome_path:
            print(f"使用系统 Chrome：{chrome_path}")
            print(f"headless 模式：{headless}")

            browser = playwright.chromium.launch(
                executable_path=chrome_path,
                headless=headless,
                args=launch_args,
            )
        else:
            print("未找到系统 Chrome，尝试使用 channel='chrome' 启动")
            print(f"headless 模式：{headless}")

            browser = playwright.chromium.launch(
                channel="chrome",
                headless=headless,
                args=launch_args,
            )

        return playwright, browser

    except Exception:
        traceback.print_exc()
        sys.exit(1)