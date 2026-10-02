"""订单列表 / 新建 / 详情 / 提交审核 —— RPA 操作路径的第 ③~⑧ 步。

校验顺序是**按表单从上到下**的：先必填和格式，再查库（SKU 存在、库存够），
最后才是查重。理由很实际 —— 查重要查库，前面几项挂了就没必要查。把
「该来源订单号已录入」放在最后，也意味着它一旦出现就说明前面全过了，
RPA 看到这句话就能确定「这单已经录过了」，这正是幂等重试要的信号。
"""

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.deps import DbDep, OperatorDep, inject_fault, templates
from app.models import ErpInventory, ErpOrder, ErpOrderStatus

logger = logging.getLogger("erp.orders")

router = APIRouter(tags=["页面"])

#: 每页条数，写死（§4.4）。老系统不给你调页大小的地方。
PAGE_SIZE = 20

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

#: 单号撞唯一键后的重试次数。同一秒内两个请求算出同一个当日序号时，
#: 唯一索引挡下后一个，退一格重算即可（§12）。
_ORDER_NO_RETRIES = 5


async def _next_order_no(session, now: datetime | None = None) -> str:
    """`ERP + YYYYMMDD + 4 位当日序号`，例：ERP202609300001。

    单号由 ERP 生成、RPA 提交后读回 —— 这样 RPA 就必须「回到详情页读值」，
    而不是当个纯填表机（§4.5）。v1 单 Worker，用「今日最大序号 + 1」足够；
    并发下靠 uk_erp_orders_no 兜底。
    """
    prefix = f"ERP{(now or datetime.now()).strftime('%Y%m%d')}"
    last = await session.scalar(
        select(func.max(ErpOrder.erp_order_no)).where(ErpOrder.erp_order_no.like(f"{prefix}%"))
    )
    seq = int(last[-4:]) + 1 if last else 1
    return f"{prefix}{seq:04d}"


def _is_source_no_conflict(exc: IntegrityError) -> bool:
    """这个 IntegrityError 是不是「来源订单号重复」引起的？

    MySQL 的报错文本里带索引名（`Duplicate entry '...' for key
    'erp_orders.uk_erp_orders_source_no'`），据此区分两类撞键：
    来源号重复 → 该报「已录入」；单号重复 → 该换个号重试。两者处理方式相反，
    混为一谈会把并发重复录入误判成「系统繁忙」。
    """
    return "uk_erp_orders_source_no" in str(getattr(exc, "orig", exc))


@router.get("/orders")
async def orders_list(
    request: Request,
    operator: OperatorDep,
    session: DbDep,
    keyword: str = "",
    status: str = "",
    page: int = 1,
):
    page = max(page, 1)
    stmt = select(ErpOrder)
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(ErpOrder.erp_order_no.like(like), ErpOrder.source_order_no.like(like))
        )
    if status in {s.value for s in ErpOrderStatus}:
        stmt = stmt.where(ErpOrder.status == status)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    orders = (
        await session.scalars(
            stmt.order_by(ErpOrder.id.desc()).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
        )
    ).all()

    return templates.TemplateResponse(
        request,
        "orders_list.html",
        {
            "operator": operator,
            "orders": orders,
            "keyword": keyword,
            "status": status,
            "page": page,
            "page_size": PAGE_SIZE,
            "total": total,
            "has_prev": page > 1,
            "has_next": page * PAGE_SIZE < total,
        },
    )


@router.get("/orders/new")
async def order_new_page(request: Request, operator: OperatorDep):
    return templates.TemplateResponse(
        request, "order_new.html", {"operator": operator, "form": {}, "error": None}
    )


@router.post("/orders")
async def create_order(
    request: Request,
    operator: OperatorDep,
    session: DbDep,
    source_order_no: str = Form(""),
    customer_name: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    product_name: str = Form(""),
    sku: str = Form(""),
    quantity: str = Form(""),
    amount: str = Form(""),
):
    """新建订单。**写操作，故障注入在这里生效**（§8.1）。

    失败一律**回到表单页并保留已填内容**：老系统不会帮你记住用户输入，
    让 RPA 重填一遍是它真实的样子，也是「RPA 必须能重放」的练习。
    """
    await inject_fault()

    form = {
        "source_order_no": source_order_no,
        "customer_name": customer_name,
        "phone": phone,
        "address": address,
        "product_name": product_name,
        "sku": sku,
        "quantity": quantity,
        "amount": amount,
    }

    def reject(message: str):
        return templates.TemplateResponse(
            request,
            "order_new.html",
            {"operator": operator, "form": form, "error": message},
            status_code=400,
        )

    source_order_no = source_order_no.strip()
    if not source_order_no:
        return reject("来源订单号不能为空")
    if len(source_order_no) > 64:
        return reject("来源订单号不能超过 64 个字符")

    customer_name = customer_name.strip()
    if not customer_name:
        return reject("客户姓名不能为空")
    if len(customer_name) > 64:
        return reject("客户姓名不能超过 64 个字符")

    if not _PHONE_RE.match(phone.strip()):
        return reject("手机号格式不正确")

    address = address.strip()
    if not address:
        return reject("收货地址不能为空")
    if len(address) > 512:
        return reject("收货地址不能超过 512 个字符")

    product_name = product_name.strip()
    if not product_name:
        return reject("商品名称不能为空")
    if len(product_name) > 255:
        return reject("商品名称不能超过 255 个字符")

    sku = sku.strip()
    if not sku:
        return reject("商品编码不能为空")

    try:
        quantity_value = int(quantity)
    except ValueError:
        return reject("数量必须是整数")
    if quantity_value < 1:
        return reject("数量必须大于 0")

    try:
        # 用 Decimal 而不是 float：财务字段不该有二进制浮点的舍入误差。
        amount_value = Decimal(amount)
    except (InvalidOperation, ValueError):
        return reject("金额格式不正确")
    if amount_value < 0:
        return reject("金额不能为负数")

    inventory = await session.scalar(select(ErpInventory).where(ErpInventory.sku == sku))
    if inventory is None:
        return reject("商品编码不存在")
    if quantity_value > inventory.quantity:
        return reject(f"库存不足，当前可用：{inventory.quantity}")

    existing = await session.scalar(
        select(ErpOrder.id).where(ErpOrder.source_order_no == source_order_no)
    )
    if existing is not None:
        return reject("该来源订单号已录入")

    for _ in range(_ORDER_NO_RETRIES):
        order = ErpOrder(
            erp_order_no=await _next_order_no(session),
            source_order_no=source_order_no,
            customer_name=customer_name,
            phone=phone.strip(),
            address=address,
            product_name=product_name,
            sku=sku,
            quantity=quantity_value,
            amount=amount_value,
            status=ErpOrderStatus.DRAFT.value,
        )
        session.add(order)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if _is_source_no_conflict(exc):
                # 上面那次查重到这里之间，有人抢先录了同一笔来源单 → 唯一索引兜住了。
                logger.warning("并发重复录入被唯一索引拦下：%s", source_order_no)
                return reject("该来源订单号已录入")
            logger.warning("单号撞键，重算：%s", exc.orig)
            continue
        logger.info("已新建 ERP 订单 %s（来源 %s）", order.erp_order_no, source_order_no)
        return RedirectResponse(url=f"/orders/{order.erp_order_no}", status_code=303)

    return reject("系统繁忙，请稍后重试")


@router.get("/orders/{erp_order_no}")
async def order_detail(
    request: Request,
    erp_order_no: str,
    operator: OperatorDep,
    session: DbDep,
    submitted: int = 0,
):
    order = await session.scalar(select(ErpOrder).where(ErpOrder.erp_order_no == erp_order_no))
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return templates.TemplateResponse(
        request,
        "order_detail.html",
        {"operator": operator, "order": order, "submitted": bool(submitted)},
    )


@router.post("/orders/{erp_order_no}/submit-review")
async def submit_review(
    request: Request, erp_order_no: str, operator: OperatorDep, session: DbDep
):
    """提交审核：DRAFT → PENDING_REVIEW。写操作，故障注入生效。"""
    await inject_fault()

    order = await session.scalar(select(ErpOrder).where(ErpOrder.erp_order_no == erp_order_no))
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")

    if order.status == ErpOrderStatus.DRAFT.value:
        order.status = ErpOrderStatus.PENDING_REVIEW.value
        await session.commit()
        logger.info("订单 %s 已提交审核", erp_order_no)

    # 已经不是 DRAFT 就**当成功**处理，不报错：RPA 回传失败后重试时会点第二次，
    # 那一次该看到「已提交审核」而不是一个错误 —— 这就是幂等（§7.2）。
    return RedirectResponse(url=f"/orders/{erp_order_no}?submitted=1", status_code=303)
