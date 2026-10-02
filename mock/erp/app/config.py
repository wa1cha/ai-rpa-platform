"""模拟 ERP 的配置。

这份配置和主服务的 `backend/app/core/config.py` **刻意不共用**。模拟 ERP 是一个
独立应用（《模拟ERP设计》§1.1 第 1 条）：独立进程、独立库、独立会话。
一旦让两边 import 同一个 Settings 模块，「两个系统隔离」就只剩一句话 ——
只要共用一个模块，早晚有人顺手把主库的连接串引进来。

唯一共用的是**同一个 .env 文件**。理由是本地开发时两份配置挨着更好维护，
而且 `MOCK_ERP_*` 和 `MYSQL_*` 的前缀本来就把两边分开了。
环境变量名与《模拟ERP设计》§8 的表格逐字对应。
"""

from pathlib import Path
from urllib.parse import quote_plus

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# mock/erp/app/config.py -> parents[3] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ErpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 应用 ----------
    app_env: str = "development"
    debug: bool = True
    mock_erp_host: str = "127.0.0.1"
    mock_erp_port: int = 8001

    # ---------- 数据库 ----------
    # 独立库。连接串优先取 MOCK_ERP_DB_URL；没配就用主库同一套 MySQL 账号，
    # 只把库名换成 mock_erp —— 本地开发不必为此多配一份账号密码。
    mock_erp_db_url: str = ""
    mock_erp_db: str = "mock_erp"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "ai_rpa"
    mysql_password: str = ""

    # ---------- ERP 操作员账号（启动时自建，见 seed.py）----------
    mock_erp_username: str = "erp_operator"
    mock_erp_password: str = ""

    # ---------- 会话 ----------
    #: 会话签名密钥。默认值是开发用的固定串 —— 上线前必须换掉，
    #: 否则任何人都能自己伪造一个已登录的 session cookie。
    mock_erp_session_secret: str = "dev-only-erp-session-secret-change-me"
    #: 会话过期时间（分钟）。过期后任意页面都被踢回 /login，RPA 得能识别这一点。
    mock_erp_session_minutes: int = 30

    # ---------- 刻意保留的摩擦（见《模拟ERP设计》§9）----------
    #: 页面跳转延时。迫使 RPA 用显式等待而不是 sleep 硬等。
    #: 测试里必须设为 0，否则每个用例白白多花几秒。
    mock_erp_page_delay_ms: int = 1500
    #: 故障注入概率，只作用在**写操作**上（保存 / 提交审核 / 审核通过）。
    #: 默认必须为 0 —— 打开后开发阶段会被随机失败折磨，只在演示和测试时调。
    mock_erp_fail_rate: float = 0.0
    mock_erp_enable_review_page: bool = True

    @field_validator("mock_erp_fail_rate")
    @classmethod
    def _clamp_fail_rate(cls, value: float) -> float:
        """概率必须在 [0, 1]。

        越界的值多半是「把 20% 写成了 20」。静默按 1.0 处理会让人以为
        注入没生效（每次必失败），不如直接报错。
        """
        if not 0.0 <= value <= 1.0:
            raise ValueError("MOCK_ERP_FAIL_RATE 必须在 0 和 1 之间（0.2 表示 20%）")
        return value

    @property
    def db_url(self) -> str:
        """异步 SQLAlchemy 连接串。

        密码做 URL 编码的理由同主服务：密码里出现 @ : / 时连接串会被解析错，
        而这类 bug 只在特定密码下复现。
        """
        if self.mock_erp_db_url:
            return self.mock_erp_db_url
        return (
            f"mysql+asyncmy://{self.mysql_user}:{quote_plus(self.mysql_password)}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mock_erp_db}?charset=utf8mb4"
        )

    @property
    def page_delay_seconds(self) -> float:
        return self.mock_erp_page_delay_ms / 1000.0

    @property
    def session_max_age_seconds(self) -> int:
        return self.mock_erp_session_minutes * 60


settings = ErpSettings()
