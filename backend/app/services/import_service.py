"""Excel/CSV 订单导入 —— 见《API接口设计》§6.3。

导入流程（四道关卡，前两道完全不碰数据库）：

    ① 解析文件        表头不对 / 打不开      → 422，不建批次记录
    ② 逐行格式校验    收集**全部**错误行
    ③ 文件内订单号查重 收集**全部**冲突行
    ④ 数据库查重 → 开事务插入 → COMMIT
                      UNIQUE 索引兜底

②③ 刻意放在事务**之外**：它们是纯内存判断，不该占用数据库事务和行锁。
只有确定整批干净，才值得去开一个事务。
"""

import csv
import io
import re
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ImportBatchStatus, OrderStatus
from app.core.exceptions import ExcelParseError, NotFoundError
from app.models.import_batch import ImportBatch
from app.repositories.import_batch_repository import ImportBatchRepository
from app.repositories.order_repository import OrderRepository
from app.schemas.common import Page, PageParams
from app.schemas.import_batch import ImportBatchDetail, ImportBatchListItem
from app.schemas.order import ImportErrorItem, OrderImportResult

#: 上传大小上限，与《API接口设计》§6.3 的 10 MB 对齐。
MAX_FILE_BYTES = 10 * 1024 * 1024

#: 行数上限。不设的话，一个几十万行的文件会把请求线程和内存一起拖死 ——
#: 这类问题在演示环境永远遇不到，上线第一个大文件就炸。
MAX_ROWS = 5000

#: Excel 中文表头 → 数据库字段名。
#: 放在这里而不是散在代码里，是为了改表头时只有一个地方要动。
HEADER_MAP: dict[str, str] = {
    "订单号": "order_no",
    "下单时间": "ordered_at",
    "客户姓名": "customer_name",
    "电话": "phone",
    "收货地址": "address",
    "商品名称": "product_name",
    "SKU": "sku",
    "数量": "quantity",
    "金额": "amount",
    "买家留言": "buyer_message",
    "卖家备注": "seller_note",
}

REQUIRED_HEADERS = (
    "订单号",
    "下单时间",
    "客户姓名",
    "电话",
    "收货地址",
    "商品名称",
    "SKU",
    "数量",
    "金额",
)

PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")

#: 允许的时间写法。真实导出文件里这几种都见过，与其让用户改文件，
#: 不如多认几种格式。
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
)

#: 字段长度上限，与建表语句一致。在这里先拦一道，避免把「数据太长」
#: 变成一条数据库层的 500 错误。
_MAX_LENGTH = {
    "order_no": 64,
    "customer_name": 64,
    "phone": 32,
    "address": 512,
    "product_name": 255,
    "sku": 64,
    "buyer_message": 1000,
    "seller_note": 1000,
}

#: 从 MySQL 的重复键报错里抠出订单号，用于给用户一个能定位的错误信息。
_DUP_ENTRY_PATTERN = re.compile(r"Duplicate entry '([^']*)' for key")


@dataclass(slots=True)
class ParsedOrder:
    """一行通过校验的订单。字段名与 `orders` 表列名一致，可直接展开成 insert。"""

    row: int
    order_no: str
    ordered_at: datetime
    customer_name: str
    phone: str
    address: str
    product_name: str
    sku: str
    quantity: int
    amount: Decimal
    buyer_message: str | None = None
    seller_note: str | None = None

    def to_row(self, batch_id: int) -> dict[str, Any]:
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "row"}
        data["platform"] = "mock"
        data["status"] = OrderStatus.IMPORTED
        data["import_batch_id"] = batch_id
        return data


# ============================================================
# 取值辅助 —— Excel 的单元格类型很杂，这类转换写错会静默出错数据
# ============================================================


def _clean_str(value: Any) -> str | None:
    """转成字符串。

    最容易踩的坑：**手机号/订单号在 Excel 里是数字**。数字单元格读出来是
    float，`str(13800008888.0)` 会得到 `'13800008888.0'`，看起来对，
    但存进库就是脏数据。所以整数值的 float 必须走 int 转换。
    """
    if value is None:
        return None
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    # openpyxl 会把日期单元格直接解析成 datetime，不用再猜格式。
    if isinstance(value, datetime):
        return value
    text = _clean_str(value)
    if not text:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，必须先挡掉
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = _clean_str(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        # 必须经过 str：Decimal(0.1) 会得到 0.1000000000000000055511151231257827，
        # 因为二进制浮点本身就不精确。Decimal("0.1") 才是干净的 0.1。
        value = repr(value)
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


# ============================================================
# 读文件
# ============================================================


def _decode_csv(content: bytes) -> str:
    """CSV 解码。

    先试 `utf-8-sig`（带 BOM 的 UTF-8，Excel 另存为 CSV 的默认输出），
    再退到 `gbk` —— 中文 Excel 在简体系统上导出的 CSV 常常是 GBK，
    只认 UTF-8 的话用户会看到满屏乱码却不知道为什么。
    """
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExcelParseError("文件编码无法识别，请另存为 UTF-8 或 GBK 编码的 CSV")


def _read_raw_rows(filename: str, content: bytes) -> list[tuple[int, list[Any]]]:
    """读出 `[(文件行号, 该行所有单元格), ...]`，表头行不包含在内。

    行号用**文件里的真实行号**（表头是第 1 行，第一条数据是第 2 行），
    这样用户拿到报错能直接定位到 Excel 的那一行。
    """
    lower = filename.lower()
    if lower.endswith(".csv"):
        reader = csv.reader(io.StringIO(_decode_csv(content)))
        return [(idx, row) for idx, row in enumerate(reader, start=1)][1:]
    if lower.endswith((".xlsx", ".xlsm")):
        try:
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:
            raise ExcelParseError(f"无法打开 Excel 文件：{exc}") from exc
        sheet = workbook.active
        # data_only=True 让公式单元格返回缓存的结果值，而不是公式本身。
        return [
            (idx, list(row))
            for idx, row in enumerate(sheet.iter_rows(values_only=True), start=1)
        ][1:]
    raise ExcelParseError("只支持 .xlsx / .xlsm / .csv 格式")


def _read_header(filename: str, content: bytes) -> tuple[list[str], list[Any]]:
    """返回 `(表头字段名列表, 原始表头单元格)`，校验表头是否存在、是否齐全。"""
    lower = filename.lower()
    if lower.endswith(".csv"):
        reader = csv.reader(io.StringIO(_decode_csv(content)))
        raw_header = next(reader, None)
    else:
        try:
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:
            raise ExcelParseError(f"无法打开 Excel 文件：{exc}") from exc
        raw_header = next(workbook.active.iter_rows(values_only=True), None)

    if not raw_header:
        raise ExcelParseError("文件是空的，读不到表头行")

    headers = [_clean_str(cell) or "" for cell in raw_header]
    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if missing:
        raise ExcelParseError(
            f"表头缺少必需列：{'、'.join(missing)}。"
            f"当前表头为：{'、'.join(h for h in headers if h)}"
        )
    # 多余的列（用户自己加的「备注2」之类）不阻断导入 —— 映射时按表头查表，
    # 认不出来的列自然不会进 `data`，为它拒绝整个文件不值得。
    return headers, raw_header


# ============================================================
# 校验
# ============================================================


def _validate_row(row_no: int, data: dict[str, Any]) -> tuple[ParsedOrder | None, list[str]]:
    """校验一行，返回 `(解析结果, 该行所有错误原因)`。

    一次返回**全部**错误而不是遇到第一个就返回：用户改 Excel 时希望一次
    看到所有问题，而不是改一处、传一次、再报下一个。
    """
    errors: list[str] = []

    order_no = _clean_str(data.get("order_no"))
    if not order_no:
        errors.append("订单号缺失")
    elif len(order_no) > _MAX_LENGTH["order_no"]:
        errors.append(f"订单号超过 {_MAX_LENGTH['order_no']} 字符")

    ordered_at = _to_datetime(data.get("ordered_at"))
    if ordered_at is None:
        errors.append("下单时间缺失或格式无法识别")

    customer_name = _clean_str(data.get("customer_name"))
    if not customer_name:
        errors.append("客户姓名缺失")
    elif len(customer_name) > _MAX_LENGTH["customer_name"]:
        errors.append(f"客户姓名超过 {_MAX_LENGTH['customer_name']} 字符")

    phone = _clean_str(data.get("phone"))
    if not phone:
        errors.append("电话缺失")
    elif not PHONE_PATTERN.match(phone):
        errors.append("手机号格式错误（应为 11 位，1 开头）")

    address = _clean_str(data.get("address"))
    if not address:
        errors.append("收货地址缺失")
    elif len(address) > _MAX_LENGTH["address"]:
        errors.append(f"收货地址超过 {_MAX_LENGTH['address']} 字符")

    product_name = _clean_str(data.get("product_name"))
    if not product_name:
        errors.append("商品名称缺失")
    elif len(product_name) > _MAX_LENGTH["product_name"]:
        errors.append(f"商品名称超过 {_MAX_LENGTH['product_name']} 字符")

    sku = _clean_str(data.get("sku"))
    if not sku:
        errors.append("SKU 缺失")
    elif len(sku) > _MAX_LENGTH["sku"]:
        errors.append(f"SKU 超过 {_MAX_LENGTH['sku']} 字符")

    quantity = _to_int(data.get("quantity"))
    if quantity is None:
        errors.append("数量缺失或不是整数")
    elif quantity < 1:
        errors.append("数量必须 ≥ 1")

    amount = _to_decimal(data.get("amount"))
    if amount is None:
        errors.append("金额缺失或不是数字")
    elif amount < 0:
        errors.append("金额不能为负")

    buyer_message = _clean_str(data.get("buyer_message"))
    if buyer_message and len(buyer_message) > _MAX_LENGTH["buyer_message"]:
        errors.append(f"买家留言超过 {_MAX_LENGTH['buyer_message']} 字符")

    seller_note = _clean_str(data.get("seller_note"))
    if seller_note and len(seller_note) > _MAX_LENGTH["seller_note"]:
        errors.append(f"卖家备注超过 {_MAX_LENGTH['seller_note']} 字符")

    if errors:
        return None, errors

    return (
        ParsedOrder(
            row=row_no,
            order_no=order_no,  # type: ignore[arg-type]
            ordered_at=ordered_at,  # type: ignore[arg-type]
            customer_name=customer_name,  # type: ignore[arg-type]
            phone=phone,  # type: ignore[arg-type]
            address=address,  # type: ignore[arg-type]
            product_name=product_name,  # type: ignore[arg-type]
            sku=sku,  # type: ignore[arg-type]
            quantity=quantity,  # type: ignore[arg-type]
            amount=amount,  # type: ignore[arg-type]
            buyer_message=buyer_message,
            seller_note=seller_note,
        ),
        [],
    )


# ============================================================
# 服务
# ============================================================


class ImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.orders = OrderRepository(session)
        self.batches = ImportBatchRepository(session)

    async def import_file(
        self,
        *,
        filename: str,
        content: bytes,
        uploaded_by: int,
        dry_run: bool = False,
    ) -> OrderImportResult:
        # ---------- ① 解析：连表头都读不出来就直接 422，不建批次记录 ----------
        if len(content) > MAX_FILE_BYTES:
            raise ExcelParseError(
                f"文件超过 {MAX_FILE_BYTES // 1024 // 1024} MB 上限"
            )
        headers, _ = _read_header(filename, content)
        raw_rows = _read_raw_rows(filename, content)

        if len(raw_rows) > MAX_ROWS:
            raise ExcelParseError(f"文件超过 {MAX_ROWS} 行上限")

        total_rows = len(raw_rows)
        if total_rows == 0:
            raise ExcelParseError("文件里没有数据行")

        # ---------- ② 逐行格式校验 ----------
        parsed: list[ParsedOrder] = []
        errors: list[ImportErrorItem] = []
        for row_no, cells in raw_rows:
            data = {
                field: (cells[idx] if idx < len(cells) else None)
                for idx, header in enumerate(headers)
                if header in HEADER_MAP
                for field in [HEADER_MAP[header]]
            }
            order, reasons = _validate_row(row_no, data)
            order_no = _clean_str(data.get("order_no"))
            for reason in reasons:
                errors.append(ImportErrorItem(row=row_no, order_no=order_no, reason=reason))
            if order is not None:
                parsed.append(order)

        if errors:
            return await self._fail(
                filename=filename,
                uploaded_by=uploaded_by,
                total_rows=total_rows,
                success_rows=0,
                errors=errors,
                dry_run=dry_run,
            )

        # ---------- ③ 文件内订单号查重 ----------
        seen: dict[str, int] = {}
        for order in parsed:
            first = seen.get(order.order_no)
            if first is None:
                seen[order.order_no] = order.row
            else:
                errors.append(
                    ImportErrorItem(
                        row=order.row,
                        order_no=order.order_no,
                        reason=f"订单号在文件内重复（第 {first} 行已出现）",
                    )
                )
        if errors:
            return await self._fail(
                filename=filename,
                uploaded_by=uploaded_by,
                total_rows=total_rows,
                success_rows=0,
                errors=errors,
                dry_run=dry_run,
            )

        # ---------- ④ 数据库查重（预检，只为给出好看的错误清单）----------
        existing = await self.orders.find_existing_order_nos([o.order_no for o in parsed])
        if existing:
            errors.extend(
                ImportErrorItem(row=o.row, order_no=o.order_no, reason="订单号已存在")
                for o in parsed
                if o.order_no in existing
            )
            return await self._fail(
                filename=filename,
                uploaded_by=uploaded_by,
                total_rows=total_rows,
                success_rows=0,
                errors=errors,
                dry_run=dry_run,
            )

        # ---------- dry_run：到此为止，不落库 ----------
        if dry_run:
            return OrderImportResult(
                batch_id=None,
                filename=filename,
                total_rows=total_rows,
                success_rows=total_rows,
                failed_rows=0,
                status=ImportBatchStatus.COMPLETED,
                errors=[],
            )

        return await self._commit_orders(
            filename=filename,
            uploaded_by=uploaded_by,
            total_rows=total_rows,
            parsed=parsed,
        )

    # ---------- 批次查询（见《API接口设计》§7）----------

    async def list_batches(
        self, params: PageParams, *, status: str | None = None
    ) -> Page[ImportBatchListItem]:
        rows, total = await self.batches.list_batches(params, status=status)
        items = [
            self._to_batch_item(batch, username) for batch, username in rows
        ]
        return Page(
            items=items, total=total, page=params.page, page_size=params.page_size
        )

    async def get_batch(self, batch_id: int) -> ImportBatchDetail:
        found = await self.batches.get_batch(batch_id)
        if found is None:
            raise NotFoundError("导入批次不存在")
        batch, username = found

        return ImportBatchDetail(
            **self._to_batch_item(batch, username).model_dump(),
            # 列表页不返回错误明细（一次可能几百条），详情页才给。
            errors=[
                ImportErrorItem(**item) for item in (batch.error_detail or [])
            ],
            order_ids=await self.batches.list_order_ids(batch_id),
        )

    @staticmethod
    def _to_batch_item(batch: ImportBatch, username: str | None) -> ImportBatchListItem:
        return ImportBatchListItem(
            id=batch.id,
            filename=batch.filename,
            uploaded_by=username,
            total_rows=batch.total_rows,
            success_rows=batch.success_rows,
            failed_rows=batch.failed_rows,
            status=batch.status,
            created_at=batch.created_at,
            finished_at=batch.finished_at,
        )

    async def _fail(
        self,
        *,
        filename: str,
        uploaded_by: int,
        total_rows: int,
        success_rows: int,
        errors: list[ImportErrorItem],
        dry_run: bool,
    ) -> OrderImportResult:
        """整批失败的收尾：dry_run 不落记录，否则留一条 FAILED 批次。

        **失败也要留痕**——否则「文件到底传没传成功」就只能靠猜，
        而 `import_batches` 本来就是为了回答这个问题才存在的。
        """
        failed_rows = len({e.row for e in errors})
        if dry_run:
            return OrderImportResult(
                batch_id=None,
                filename=filename,
                total_rows=total_rows,
                success_rows=success_rows,
                failed_rows=failed_rows,
                status=ImportBatchStatus.FAILED,
                errors=errors,
            )

        batch = self.batches.create(
            filename=filename, uploaded_by=uploaded_by, total_rows=total_rows
        )
        await self.session.flush()
        batch_id = batch.id
        await self.batches.finish_by_id(
            batch_id,
            status=ImportBatchStatus.FAILED,
            success_rows=success_rows,
            failed_rows=failed_rows,
            errors=[e.model_dump() for e in errors],
        )
        await self.session.commit()

        return OrderImportResult(
            batch_id=batch_id,
            filename=filename,
            total_rows=total_rows,
            success_rows=success_rows,
            failed_rows=failed_rows,
            status=ImportBatchStatus.FAILED,
            errors=errors,
        )

    async def _commit_orders(
        self,
        *,
        filename: str,
        uploaded_by: int,
        total_rows: int,
        parsed: list[ParsedOrder],
    ) -> OrderImportResult:
        """真正写库。批次记录**先**提交，订单再一起提交。"""
        batch = self.batches.create(
            filename=filename, uploaded_by=uploaded_by, total_rows=total_rows
        )
        # 先提交批次：万一下面插入订单时进程挂了，至少能查到「有人试过导这个文件」。
        await self.session.commit()
        batch_id = batch.id

        try:
            self.orders.add_all([o.to_row(batch_id) for o in parsed])
            await self.session.commit()
        except IntegrityError as exc:
            # 预检说「不重复」但插入时撞了唯一索引 —— 只可能是并发：
            # 另一个请求在同一时间导入了相同的订单号。
            # 这证明「先查后插」不是正确性保证，唯一索引才是。
            await self.session.rollback()
            errors = self._conflict_errors(exc, parsed)
            await self.batches.finish_by_id(
                batch_id,
                status=ImportBatchStatus.FAILED,
                success_rows=0,
                failed_rows=len({e.row for e in errors}),
                errors=[e.model_dump() for e in errors],
            )
            await self.session.commit()
            return OrderImportResult(
                batch_id=batch_id,
                filename=filename,
                total_rows=total_rows,
                success_rows=0,
                failed_rows=len({e.row for e in errors}),
                status=ImportBatchStatus.FAILED,
                errors=errors,
            )

        await self.batches.finish_by_id(
            batch_id,
            status=ImportBatchStatus.COMPLETED,
            success_rows=total_rows,
            failed_rows=0,
        )
        await self.session.commit()

        return OrderImportResult(
            batch_id=batch_id,
            filename=filename,
            total_rows=total_rows,
            success_rows=total_rows,
            failed_rows=0,
            status=ImportBatchStatus.COMPLETED,
            errors=[],
        )

    @staticmethod
    def _conflict_errors(
        exc: IntegrityError, parsed: list[ParsedOrder]
    ) -> list[ImportErrorItem]:
        """把数据库的唯一键报错还原成「哪一行冲突了」。

        MySQL 的报错里带着重复的值，抠出来就能定位到具体行 ——
        否则并发冲突时用户只会看到一句「数据冲突」，完全不知道该改哪一行。
        抠不出来就退回一条不指行的通用错误，总比没有好。
        """
        match = _DUP_ENTRY_PATTERN.search(str(exc.orig))
        if not match:
            return [ImportErrorItem(row=0, order_no=None, reason="订单号与已导入数据冲突")]

        conflict_no = match.group(1)
        rows = [o for o in parsed if o.order_no == conflict_no]
        if not rows:
            return [
                ImportErrorItem(row=0, order_no=conflict_no, reason="订单号与已导入数据冲突")
            ]
        return [
            ImportErrorItem(row=o.row, order_no=o.order_no, reason="订单号已存在（并发导入冲突）")
            for o in rows
        ]
