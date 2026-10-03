"""订单输入 → 送进 Prompt 的字段集合。

`OrderInput` 只保留《LLM_Prompt设计》§2.1 判定「有语义价值」的 7 个字段。
刻意**不含** customer_name / phone（与判断无关且是隐私数据）、sku（纯编码）、
platform（v1 固定 mock）—— 少送字段既省 token，也缩小数据暴露面。

这是 backend 的 `Order` 与 AI 之间的边界：backend 负责把 `Order` 映射成
`OrderInput`，本包对数据库一无所知。
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class OrderInput:
    order_no: str
    product_name: str
    quantity: int
    amount: Decimal
    address: str
    buyer_message: str | None
    seller_note: str | None


#: 渲染用中文标签，顺序与《LLM_Prompt设计》§4.2 的示例一致。
#: 用中文标签而不是字段名，是为了和 system prompt 里「判定标准」的措辞对齐，
#: 减少模型的理解负担。
_LABELS: tuple[tuple[str, str], ...] = (
    ("order_no", "订单号"),
    ("product_name", "商品名称"),
    ("quantity", "数量"),
    ("amount", "金额"),
    ("address", "收货地址"),
    ("buyer_message", "买家留言"),
    ("seller_note", "卖家备注"),
)

#: 空值统一渲染成「无」而不是留空 —— 留空会让模型以为数据缺失（§4.2 要点）。
_EMPTY = "无"


def render(order: OrderInput) -> dict[str, str]:
    """把 `OrderInput` 摊成 `{中文标签: 字符串}`，供 prompt 模板逐行展开。

    金额按两位小数渲染，保留 CSV 里的「299.00」形态 —— 送去给模型看的数字
    要和业务口径一致，不能变成 float 的 `299.0`。
    """
    rendered: dict[str, str] = {}
    for field, label in _LABELS:
        value = getattr(order, field)
        if value is None:
            text = ""
        elif isinstance(value, Decimal):
            text = f"{value:.2f}"
        else:
            text = str(value)
        rendered[label] = text.strip() or _EMPTY
    return rendered
