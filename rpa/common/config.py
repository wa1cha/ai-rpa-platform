"""RPA Worker 的配置。

和主服务 `backend/app/core/config.py`、模拟 ERP `mock/erp/app/config.py` 一样，
读的都是**根目录同一个 .env**。三份配置各读各的键，字段名与 .env 里的键逐一对应，
不互相 import —— Worker 是要部署到另一台机器上的，让它依赖主服务的配置模块，
等于把那台机器也拖上整个 backend 的依赖树。

Worker 需要的配置分三类：

  1. **怎么找到主平台**：`RPA_API_BASE_URL`（留空则按 `APP_HOST`/`APP_PORT` 推导）
  2. **自己的身份**：`RPA_WORKER_NAME` + `WORKER_USERNAME`/`WORKER_PASSWORD`
  3. **怎么操作浏览器**：无头开关、各类超时、轮询与心跳间隔

**模拟 ERP 的地址不在这里** —— 它由服务端在 claim 响应里下发（`instruction.erp_url`）。
理由见《API接口设计》§10.1：换环境只改服务端一处，Worker 端不用重新配置。
但 ERP 的**账号口令必须是本地的**，服务端不会把别人的登录凭据发下来。
"""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# rpa/common/config.py -> parents[2] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RpaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 主平台 ----------
    #: 主平台 API 的根地址。留空则按 APP_HOST/APP_PORT 推导开发机地址 ——
    #: 这样本地开发不必为了 Worker 再写一遍 host/port。
    rpa_api_base_url: str = ""
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    # ---------- 身份 ----------
    #: 写入 tasks.claimed_by，也是心跳/回传时的自称。**必须和领任务时用的一致**，
    #: 否则服务端会认为「这活不归你」（heartbeat 直接 cancel、result 直接 409）。
    rpa_worker_name: str = "rpa-worker-01"
    worker_username: str = ""
    worker_password: str = ""

    # ---------- 模拟 ERP 的登录凭据 ----------
    #: 地址不在这里（服务端下发），但账号口令只能本地配。
    mock_erp_username: str = "erp_operator"
    mock_erp_password: str = ""

    # ---------- 浏览器 ----------
    #: 无头。默认 True。演示时用 `--headed` 打开，能看着它一步步点。
    rpa_headless: bool = True

    #: 页面导航超时。必须**明显大于**模拟 ERP 的页面延时（默认 1500ms），
    #: 否则每一次跳转都会假超时。15 秒留了 10 倍余量。
    rpa_nav_timeout_ms: int = 15000
    #: 单次操作（等元素出现、点击）的超时。
    rpa_action_timeout_ms: int = 10000

    # ---------- 调度 ----------
    #: claim 的长轮询秒数。>0 时没活会挂起等，避免空转打接口。
    #: 上限 30 是服务端定的（《API接口设计》§10.1），这里跟着定一样的上限。
    rpa_poll_wait_seconds: int = 20
    #: 心跳间隔。服务端的僵尸判定阈值是 300 秒（zombie_timeout_seconds），
    #: 30 秒一次意味着连丢 9 次心跳才会被误判，容忍度足够。
    rpa_heartbeat_seconds: int = 30

    @field_validator("rpa_poll_wait_seconds")
    @classmethod
    def _poll_wait_within_server_limit(cls, value: int) -> int:
        """服务端把 wait_seconds 限制在 0~30，超了直接 400。

        在配置阶段就拦下来，比等第一次 claim 收到 400 再回头查配置快得多。
        """
        if not 0 <= value <= 30:
            raise ValueError("RPA_POLL_WAIT_SECONDS 必须在 0 和 30 之间（服务端上限）")
        return value

    @field_validator("rpa_heartbeat_seconds")
    @classmethod
    def _heartbeat_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("RPA_HEARTBEAT_SECONDS 必须大于 0")
        return value

    @property
    def api_base_url(self) -> str:
        """主平台 API 根地址，含 `/api/v1` 后缀。"""
        if self.rpa_api_base_url:
            return self.rpa_api_base_url.rstrip("/")
        return f"http://{self.app_host}:{self.app_port}/api/v1"


settings = RpaSettings()
