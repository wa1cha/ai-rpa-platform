"""客户黑名单 —— 仓储层 + 与硬规则引擎的整链路。

黑名单是四条硬规则里**唯一需要查库**的一条（其余三条都只看订单自身的字段）。
所以它的测试重点是把「库里的行」和「规则引擎的判定」接起来验：
仓储真的只取生效的行、规则引擎拿这个集合命中后确实升级到 HIGH。

v1 的匹配键是手机号（《数据库设计》§8.1）—— 没有 customer 主表，
黑名单就是一份「手机号集合」。
"""

import pytest
from sqlalchemy import select

from app.models.customer_blacklist import CustomerBlacklist
from app.repositories.customer_blacklist_repository import CustomerBlacklistRepository
from app.services import rule_engine

pytestmark = pytest.mark.integration


async def _add(session, phone: str, *, is_active: bool = True, **overrides):
    row = CustomerBlacklist(
        phone=phone,
        customer_name=overrides.get("customer_name"),
        reason=overrides.get("reason"),
        is_active=is_active,
    )
    session.add(row)
    await session.commit()
    return row


# ============================================================
# 仓储
# ============================================================


async def test_repository_returns_only_active_phones(db_session):
    """停用比删除好回溯（表设计注释），但停用的行绝不能再参与命中判定。"""
    await _add(db_session, "13800001111", is_active=True)
    await _add(db_session, "13800002222", is_active=False)

    phones = await CustomerBlacklistRepository(db_session).list_active_phones()

    assert phones == {"13800001111"}


async def test_repository_returns_an_empty_set_when_the_table_is_empty(db_session):
    assert await CustomerBlacklistRepository(db_session).list_active_phones() == set()


# ============================================================
# 整链路：库里的行 → 规则引擎命中 → HIGH
# ============================================================


async def test_blacklisted_phone_drives_the_hard_rule_to_high(db_session, make_order):
    """整条链路：从库里查出生效手机号 → 喂给规则引擎 → 那一单升为 HIGH。

    这条测试是「黑名单不是摆设」的证据：它跨过了仓储和规则引擎两层，
    而不是各自 mock 一个集合 / 一个订单。
    """
    await _add(db_session, "13800007777", reason="历史退款纠纷")
    order = await make_order(phone="13800007777")

    phones = await CustomerBlacklistRepository(db_session).list_active_phones()
    result = rule_engine.evaluate(order, blacklisted_phones=phones)

    assert result.risk_level == "HIGH"
    assert "黑名单" in result.risk_reason


async def test_a_normal_phone_is_not_flagged(db_session, make_order):
    await _add(db_session, "13800007777")
    order = await make_order(phone="13900000000")

    phones = await CustomerBlacklistRepository(db_session).list_active_phones()
    result = rule_engine.evaluate(order, blacklisted_phones=phones)

    assert result.risk_level == "LOW"


async def test_a_deactivated_entry_no_longer_flags(db_session, make_order):
    """同一条链路，但那条黑名单已停用 —— 不该命中。把 is_active 的作用落到链路上。"""
    await _add(db_session, "13800007777", is_active=False)
    order = await make_order(phone="13800007777")

    phones = await CustomerBlacklistRepository(db_session).list_active_phones()
    result = rule_engine.evaluate(order, blacklisted_phones=phones)

    assert result.risk_level == "LOW"


async def test_seed_script_would_insert_a_unique_phone(db_session):
    """手机号是唯一键 —— 种子里那条 `13800007777` 重复插入会被数据库拒绝。

    这条不是在测脚本本身（脚本的幂等由 `count(*)==0` 前置判断保证），
    而是钉住「手机的 UNIQUE 约束真的在库里生效」这个前提。
    """
    await _add(db_session, "13800007777")

    with pytest.raises(Exception):
        await _add(db_session, "13800007777")

    await db_session.rollback()
    count = (
        await db_session.execute(
            select(CustomerBlacklist).where(CustomerBlacklist.phone == "13800007777")
        )
    ).scalars().all()
    assert len(count) == 1
