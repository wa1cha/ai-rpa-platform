"""纯逻辑测试 —— 不连数据库、不连网络。

单列一个文件是因为这个模块里所有测试都没有 `integration` 标记，于是 conftest
那个 autouse 的清库夹具不会碰它们。**没有装 MySQL 的机器也能跑这一批** ——
这是「unit 层」存在的全部意义。所以它们必须待在**不带模块级 marker** 的文件里，
而不是混进上面那几个（那里每条都会被标成 integration，连 sync 测试也会被
要求 await 一个异步夹具）。
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.config import ErpSettings
from app.security import hash_password, verify_password

pytestmark = pytest.mark.unit


# ============================================================
# 配置
# ============================================================


def test_page_delay_seconds_converts_milliseconds():
    assert ErpSettings(mock_erp_page_delay_ms=1500).page_delay_seconds == 1.5


def test_session_max_age_is_minutes_in_seconds():
    assert ErpSettings(mock_erp_session_minutes=30).session_max_age_seconds == 1800


def test_db_url_is_derived_from_the_mysql_settings():
    """没配 MOCK_ERP_DB_URL 时，复用主库那套账号、只换库名。

    这样本地开发不必为了模拟 ERP 再维护一份账号密码。
    """
    settings = ErpSettings(
        mock_erp_db_url="",
        mock_erp_db="mock_erp_test",
        mysql_user="ai_rpa",
        mysql_password="p@ss:word/1",  # 含需转义的字符
        mysql_host="127.0.0.1",
        mysql_port=3307,
    )

    url = settings.db_url

    assert url.startswith("mysql+asyncmy://ai_rpa:")
    assert url.endswith("@127.0.0.1:3307/mock_erp_test?charset=utf8mb4")
    # 密码里的 @ : / 必须被百分号编码，否则连接串会被解析错 ——
    # 而这类 bug 只在特定密码下才复现。
    assert "p@ss:word/1" not in url


def test_explicit_db_url_wins():
    settings = ErpSettings(mock_erp_db_url="mysql+asyncmy://x:y@h:1/z")

    assert settings.db_url == "mysql+asyncmy://x:y@h:1/z"


def test_fail_rate_defaults_to_zero():
    """默认 0 是硬要求：不为 0 的话开发阶段会被随机失败折磨（§8.1）。"""
    assert ErpSettings().mock_erp_fail_rate == 0.0


@pytest.mark.parametrize("value", [0.0, 0.2, 1.0])
def test_fail_rate_accepts_valid_probabilities(value):
    assert ErpSettings(mock_erp_fail_rate=value).mock_erp_fail_rate == value


@pytest.mark.parametrize("value", [-0.1, 1.5, 20])
def test_fail_rate_rejects_out_of_range_values(value):
    """越界的值多半是「把 20% 写成了 20」。

    静默按 1.0 处理会让人以为注入没生效（每次都失败），不如直接拒绝启动。
    """
    with pytest.raises(ValueError):
        ErpSettings(mock_erp_fail_rate=value)


# ============================================================
# 口令哈希
# ============================================================


def test_hash_and_verify_roundtrip():
    stored = hash_password("hunter2")

    assert stored != "hunter2"  # 绝不出现明文
    assert verify_password("hunter2", stored) is True
    assert verify_password("hunter3", stored) is False


def test_hash_is_salted():
    """同一个口令两次哈希结果不同 —— bcrypt 自带盐，这是对的，别去「修」。"""
    assert hash_password("same") != hash_password("same")


def test_verify_returns_false_for_a_damaged_hash():
    """认证流程不该因为库里有条脏数据就 500。"""
    assert verify_password("whatever", "not-a-bcrypt-hash") is False


# ============================================================
# 撞键归因
# ============================================================


def test_source_conflict_is_told_apart_from_an_order_no_conflict():
    """两类撞键处理方式相反，必须能分开。

    来源号重复 → 回「该来源订单号已录入」（RPA 据此判定为幂等成功）；
    单号重复   → 换个号重试。认错了就把「已被录入」误报成「系统繁忙」。
    """
    from app.routes.orders import _is_source_no_conflict

    def _integrity_error(text: str) -> IntegrityError:
        return IntegrityError("INSERT ...", {}, Exception(text))

    assert (
        _is_source_no_conflict(
            _integrity_error(
                "Duplicate entry 'MOCK-1' for key 'erp_orders.uk_erp_orders_source_no'"
            )
        )
        is True
    )
    assert (
        _is_source_no_conflict(
            _integrity_error("Duplicate entry 'ERP1' for key 'erp_orders.uk_erp_orders_no'")
        )
        is False
    )
