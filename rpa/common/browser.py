"""浏览器会话：启动 Chromium、登录模拟 ERP、按需重登、失败截图。

## 为什么是同步 API

Playwright 的同步 API 绑定在创建它的线程上，而且**不能在 asyncio 事件循环里
运行**。本项目 v1 是单 Worker、一次只处理一个任务，浏览器操作本来就是串行的 ——
用同步 API 换来的是线性、好读、好断点的代码，异步在这里带不来任何并发收益。
唯一需要「同时」进行的是心跳，而心跳只做 HTTP、完全不碰浏览器，所以拆到
另一个线程是安全的（见 `rpa/main.py`）。

## 会话复用

浏览器和登录会话**全程复用**：启动时开一次 Chromium，第一次任务时登录一次，
之后的任务沿用同一个 context（cookie 就在里面）。会话过期时任何页面都会被
303 踢回 `/login`，`ensure_logged_in()` 会在那之后再登一次。

这样做的代价是：§3.1 那条路径里的「① 打开 /login」只在启动时演示一次。
收益是每单省掉一次登录（在 1.5 秒页面延时下，一次登录就是两个页面往返）。
长跑 Worker 的真实形态就是这样 —— 没人会每单重登一次。
"""

from __future__ import annotations

import logging

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from rpa.common import errors
from rpa.common.config import RpaSettings, settings as default_settings
from rpa.erp.pages import DashboardPage, LoginPage

logger = logging.getLogger(__name__)


class BrowserSession:
    """持有 Chromium / context / page 三件套，以及「登录状态」。"""

    def __init__(self, settings: RpaSettings | None = None) -> None:
        self._settings = settings or default_settings
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        #: 登录过的 ERP 根地址。来自服务端下发的 `instruction.erp_url`，
        #: 所以第一次登录之前是 None —— 我们不会在拿到任务前瞎猜地址。
        self._erp_url: str | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        mode = "有头（演示模式）" if not self._settings.rpa_headless else "无头"
        logger.info("启动 Chromium（%s）", mode)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self._settings.rpa_headless
        )
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        # 默认超时设成 action 超时；导航另设（页面延时让它必须更长）。
        self._page.set_default_timeout(self._settings.rpa_action_timeout_ms)
        self._page.set_default_navigation_timeout(self._settings.rpa_nav_timeout_ms)

    def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    closer.close()
                except PlaywrightError:  # pragma: no cover - 关闭时的噪声不值得中断
                    logger.debug("关闭浏览器资源时出错，忽略", exc_info=True)
        if self._playwright is not None:
            self._playwright.stop()
        self._page = self._context = self._browser = self._playwright = None

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------- 访问器 ----------

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("浏览器还没启动，先调用 start()")
        return self._page

    @property
    def erp_url(self) -> str:
        if self._erp_url is None:
            raise RuntimeError("还没登录过模拟 ERP，地址未知")
        return self._erp_url

    # ---------- 登录 ----------

    def ensure_logged_in(self, erp_url: str) -> None:
        """保证「现在处于已登录状态」。

        三种情况：
          · 还没登录过 → 登录
          · 当前页面停在 `/login` → 会话过期了，重登一次
          · 其他 → 什么都不做（正常情况，复用已有会话）
        """
        erp_url = erp_url.rstrip("/")
        if self._erp_url != erp_url:
            # 换了环境（管理员改了服务端配置）就重新登录，别把旧 cookie 带过去。
            logger.info("模拟 ERP 地址为 %s，开始登录", erp_url)
            self._erp_url = erp_url
            self._login()
            return

        login_page = LoginPage(self.page, self._settings, erp_url)
        if login_page.is_current():
            logger.warning("会话已失效（页面被踢回 /login），重新登录")
            self._login()

    def _login(self) -> None:
        """登录并确认真的进去了。

        登录失败在模拟 ERP 里**不是** HTTP 401，而是一个 200 的登录页加一句
        「用户名或密码错误」。所以这里不能看状态码，只能看「有没有跳到 /dashboard」。
        """
        assert self._erp_url is not None
        login_page = LoginPage(self.page, self._settings, self._erp_url)
        login_page.open()
        login_page.submit(
            self._settings.mock_erp_username,
            self._settings.mock_erp_password,
        )

        error_text = login_page.error_text()
        if error_text:
            raise errors.LoginFailed(f"登录模拟 ERP 失败：{error_text}")

        # 登录成功后应该落在 /dashboard。没跳过去说明既没报错也没成功，
        # 这属于「页面结构变了」这一类，值得单独报出来。
        dashboard = DashboardPage(self.page, self._settings, self._erp_url)
        if not self.page.url.rstrip("/").endswith(dashboard.PATH):
            raise errors.LoginFailed(
                f"登录后没有跳到 {dashboard.PATH}，当前 URL 是 {self.page.url}"
            )
        logger.info("已登录模拟 ERP：%s", self._settings.mock_erp_username)

    # ---------- 失败截图 ----------

    def screenshot(self, *, full_page: bool = True) -> bytes | None:
        """拍当前页面，返回 PNG 字节。拿不到就返回 None。

        `page.screenshot()` 直接给 bytes —— **不需要临时文件**，也就没有
        「截图落在哪、什么时候清」这些问题。内存里拿到的字节直接进 multipart。

        截图失败本身**不该让整个任务失败**：它只是给排查提供便利的附件，
        拍不到就算了，主流程该报什么错还报什么错。
        """
        if self._page is None:
            return None
        try:
            return self.page.screenshot(full_page=full_page, type="png")
        except PlaywrightTimeoutError:
            logger.warning("截图超时，跳过（不影响失败原因的判定）")
        except PlaywrightError:
            logger.warning("截图失败，跳过（不影响失败原因的判定）", exc_info=True)
        return None
