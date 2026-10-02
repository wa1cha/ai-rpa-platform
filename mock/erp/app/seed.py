"""启动时自建最小可用数据。

为什么是代码而不是 `database/seed/02_erp_users.sql`：**bcrypt 哈希算不出来**。
把口令的哈希写死在 .sql 里等于把口令固化进版本库，而且改口令要改代码。
主服务的 `scripts/seed_admin.py` 是同一个理由的同一个解法。

库存则用 `database/seed/03_erp_inventory.sql` 作为「正式」种子；这里只在
表**完全为空**时补一份等价的，让一个空库 clone 下来就能直接登录、直接演示。
已经有数据时一律不动 —— 否则有人手工把 SKU-003 调回 0 再启动服务就被覆盖了。
"""

import logging

from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import ErpInventory, ErpUser
from app.security import hash_password

logger = logging.getLogger("erp.seed")

#: 空库时的兜底库存。与 `database/seed/03_erp_inventory.sql` 保持一致。
#: SKU-003 故意为 0 —— 「一切正常」的样例数据测不出「库存不足」那条分支。
_FALLBACK_INVENTORY: list[tuple[str, int]] = [
    ("SKU-001", 50),  # 儿童积木套装
    ("SKU-002", 30),  # 不锈钢保温杯
    ("SKU-003", 0),  # 蓝牙耳机  ← 故意为 0
    ("SKU-004", 15),  # 机械键盘
    ("SKU-005", 100),  # 无线鼠标
    ("SKU-006", 8),  # 显示器支架
    ("SKU-007", 25),  # 移动电源
]


async def _seed_operator(session) -> None:
    exists = await session.scalar(select(func.count()).select_from(ErpUser))
    if exists:
        return
    if not settings.mock_erp_password:
        # 不建一个登不进去的账号 —— 那比没有账号更难查。
        logger.error(
            "MOCK_ERP_PASSWORD 未设置，跳过创建 ERP 操作员；ERP 将无法登录。"
            "请在 .env 里设置 MOCK_ERP_PASSWORD"
        )
        return
    session.add(
        ErpUser(
            username=settings.mock_erp_username,
            password_hash=hash_password(settings.mock_erp_password),
        )
    )
    logger.info("已创建 ERP 操作员：%s", settings.mock_erp_username)


async def _seed_inventory(session) -> None:
    exists = await session.scalar(select(func.count()).select_from(ErpInventory))
    if exists:
        return
    session.add_all(
        ErpInventory(sku=sku, quantity=quantity) for sku, quantity in _FALLBACK_INVENTORY
    )
    logger.info("已写入 %d 条兜底库存", len(_FALLBACK_INVENTORY))


async def seed_erp_data() -> None:
    """lifespan 里调用。任何一步失败都不该让应用起不来 ——
    ERP 起不来的话 RPA 整条链路都断了，而种子数据缺失顶多是登录不上。
    """
    async with AsyncSessionLocal() as session:
        await _seed_operator(session)
        await _seed_inventory(session)
        await session.commit()
