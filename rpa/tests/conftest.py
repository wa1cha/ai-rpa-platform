"""RPA Worker 测试的夹具。

## 这个文件为什么必须独立于根目录的 `tests/conftest.py`

根目录那个 conftest 里有一个 **autouse 的 async 夹具**（`clean_state`），
它靠 pytest-asyncio 跑，而 pytest-asyncio 会给异步夹具拉起一个**事件循环**。

Playwright 的同步 API 明确禁止在事件循环里运行 —— 一旦被套进循环，
`sync_playwright()` 会直接抛 `Error: It looks like you are using Playwright Sync API
inside an asyncio loop`。所以这边不能、也不需要任何 autouse 异步夹具：
本套件全是同步测试，一个事件循环都不建。

（实际上根 conftest 也不会被收集到 —— pytest 只收集 rootdir 之下的 conftest，
而本套件的 rootdir 是 `rpa/`。这条说明是为了让「为什么这里什么都没有」有答案，
而不是靠记住 pytest 的收集规则。）

## 关于 `unit` 那一层

`unit` 测试**不**需要任何服务：它们只测选择器契约、异常分类、配置推导、
结果组装这些纯逻辑。所以下面的 live 夹具只在 `e2e` 用得上，
unit 层必须在没起 MySQL / Redis / 模拟 ERP 的机器上也能跑绿。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import httpx
import pytest

#: rpa/tests/conftest.py -> parents[2] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = PROJECT_ROOT / ".env"

#: e2e 用的两个探针。都很快（模拟 ERP 的页面延时只作用在 text/html 的 GET 上，
#: /health 和 /api/* 都是即时的）。
_PLATFORM_HEALTH = "/api/v1/health"
_ERP_HEALTH = "/health"


def env_value(key: str, default: str = "") -> str:
    """从根目录的 `.env` 里读一个键。

    只解析 `KEY=VALUE` 这种最简单的行 —— 这里要的只是 e2e 用的管理员口令，
    为它引一个配置框架不值得。**不把 .env 当脚本执行**（和 scripts/*.sh 同一条原则）。
    """
    if not _ENV_FILE.exists():
        return default
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return default


def _probe(url: str) -> bool:
    try:
        response = httpx.get(url, timeout=3.0, trust_env=False)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


# ============================================================
# e2e 的前置：两个服务都得活着
# ============================================================


@pytest.fixture(scope="session")
def platform_url() -> str:
    """主平台根地址（不带 /api/v1）。"""
    return env_value("RPA_API_BASE_URL") or (
        f"http://{env_value('APP_HOST', '127.0.0.1')}:{env_value('APP_PORT', '8000')}"
    )


@pytest.fixture(scope="session")
def erp_url() -> str:
    """模拟 ERP 根地址。"""
    return env_value("MOCK_ERP_BASE_URL", "http://127.0.0.1:8001")


@pytest.fixture(scope="session")
def live_services(platform_url: str, erp_url: str) -> None:
    """两个服务都不在就**跳过**，而不是报一堆连接错误。

    跳过而不是失败是刻意的：`-m e2e` 就该「没起服务 = 跳过，起了服务 = 真跑」。
    失败会让人以为代码坏了，而实际上只是忘了 `./scripts/start.sh`。
    """
    missing = []
    if not _probe(f"{platform_url}{_PLATFORM_HEALTH}"):
        missing.append(f"主平台 {platform_url}（先跑 ./scripts/start.sh）")
    if not _probe(f"{erp_url}{_ERP_HEALTH}"):
        missing.append(f"模拟 ERP {erp_url}（先跑 ./scripts/start_mock_erp.sh）")
    if missing:
        pytest.skip("e2e 前置服务没起：" + "；".join(missing))


# ============================================================
# e2e 用的管理员 HTTP 客户端
# ============================================================


@pytest.fixture
def admin_api(platform_url: str, live_services: None) -> Iterator[httpx.Client]:
    """带管理员 JWT 的客户端，用来断言任务/执行记录的下游状态。

    为什么要管理员身份：这些断言（任务状态、execution 的 erp_order_no、
    截图能不能取回来）都只能从管理接口读 —— Worker 角色看不到它们，
    这也正是 Worker 权限最小化的体现。测试需要更宽的视角，所以换个身份看。
    """
    username = env_value("ADMIN_USERNAME", "admin")
    password = env_value("ADMIN_PASSWORD")
    if not password:
        pytest.skip("`.env` 里没有 ADMIN_PASSWORD，无法用管理员接口做断言")

    with httpx.Client(base_url=f"{platform_url}/api/v1", timeout=15.0, trust_env=False) as raw:
        response = raw.post(
            "/auth/login", json={"username": username, "password": password}
        )
        body = response.json()
        token = (body.get("data") or {}).get("access_token")
        if not token:
            pytest.skip(f"管理员登录没拿到 token（HTTP {response.status_code}）：{body!r}")
        raw.headers["Authorization"] = f"Bearer {token}"
        yield raw


# ============================================================
# 关掉 Playwright 的环境变量噪声
# ============================================================


@pytest.fixture(autouse=True)
def _no_playwright_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 Playwright 的调试输出压掉。

    这不是必须的，但没有它的时候，子进程（`seed_demo_task.py`）和驱动
    偶尔会往 stderr 打大段 node 侧的东西，把真正的失败信息顶出屏幕。
    """
    monkeypatch.delenv("DEBUG", raising=False)
    # **删掉**而不是设成 "0"：Playwright 判的是「变量在不在」，非空字符串就是真值，
    # 设成 "0" 反而等于把调试模式打开。
    monkeypatch.delenv("PWDEBUG", raising=False)
    # 让子进程里的 logging 也安静下来（backend 的 dev 配置会 echo SQL）。
    monkeypatch.delenv("SQL_ECHO", raising=False)
