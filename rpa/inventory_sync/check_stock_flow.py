"""只读流程：查一个 SKU 的库存，并判断够不够。

这个模块是「流程层能脱离订单场景复用」的证明 —— 它和
`order_sync/create_order_flow.py` 共用同一套 Page Object 和异常体系，却
完全不碰「任务」「心跳」「回传」这些概念。谁调它、拿结论去做什么，是调用方的事。

《模拟ERP设计》§4.8 给 `/inventory` 的定位就是「读表格 → 判断 → 决定是否继续」，
所以这里刻意**不停在「把数字读出来」**：数字要落进一个判断，流程才算走完。
也正因为它是纯只读的（不点按钮、不提交表单），可以放心在任何地方调用，
不用担心副作用 —— 这一点和订单流程正好形成对照。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rpa.common.config import RpaSettings
from rpa.erp.pages import InventoryPage

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page


@dataclass(frozen=True)
class StockCheck:
    """一次库存查询的结论。

    `required` 只是个参照量 —— 库存页本身不知道「你要买多少」，那是调用方的
    上下文。把两者放进同一个对象，是为了让「够不够」这个判断只有一个出处。
    """

    sku: str
    available: int
    required: int

    @property
    def sufficient(self) -> bool:
        return self.available >= self.required


def check_stock(
    page: "Page",
    settings: RpaSettings,
    base_url: str,
    sku: str,
    *,
    required: int = 1,
) -> StockCheck:
    """打开 `/inventory`，读出 `sku` 的可用库存，判断够不够 `required`。

    注意这里读到的数字**只是一个快照**：模拟 ERP 的库存下单时只校验不扣减
    （§7.2），所以两次查询之间不会有别的任务把它扣掉。真实系统里这个间隙是
    危险的 —— 查完到下单之间库存可能被抢走，正确做法是把判断交给服务端
    （这也正是 ERP 在保存时再校验一次的原因）。这里保留这个只读检查，
    是因为它对应「人工先看一眼再决定」这个真实动作。
    """
    inventory = InventoryPage(page, settings, base_url)
    inventory.open()
    available = inventory.quantity_of(sku)
    return StockCheck(sku=sku, available=available, required=required)
