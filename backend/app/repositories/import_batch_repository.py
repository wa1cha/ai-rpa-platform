"""import_batches 表的数据访问。"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from app.models.import_batch import ImportBatch
from app.models.order import Order
from app.models.user import User
from app.repositories.base import BaseRepository
from app.schemas.common import PageParams


class ImportBatchRepository(BaseRepository[ImportBatch]):
    model = ImportBatch

    def create(
        self,
        *,
        filename: str,
        uploaded_by: int | None,
        total_rows: int,
    ) -> ImportBatch:
        """建一条 PROCESSING 的批次。

        批次记录**先于订单写入**：这样即使后面整批失败，也留下了
        「什么时候、谁、传了什么文件、失败在哪」的痕迹。反过来先写订单
        再补批次，失败时就连痕迹都没有了。
        """
        batch = ImportBatch(
            filename=filename,
            uploaded_by=uploaded_by,
            total_rows=total_rows,
            success_rows=0,
            failed_rows=0,
            status="PROCESSING",
        )
        self.session.add(batch)
        return batch

    async def finish_by_id(
        self,
        batch_id: int,
        *,
        status: str,
        success_rows: int,
        failed_rows: int,
        errors: Sequence[dict[str, Any]] | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """收尾一个批次。

        刻意用 **UPDATE 语句**而不是「查出对象 → 改属性」。原因是调用时机：
        订单插入撞唯一索引后会 `session.rollback()`，而 rollback 会把 session 里
        所有对象置为过期；此时再去读 `batch.status` 之类的属性会触发懒加载，
        在异步上下文里直接抛 `MissingGreenlet`。用 UPDATE 就完全不碰对象状态。
        """
        await self.session.execute(
            update(ImportBatch)
            .where(ImportBatch.id == batch_id)
            .values(
                status=status,
                success_rows=success_rows,
                failed_rows=failed_rows,
                # 成功时显式置 None：避免「成功的批次却带着上次的错误明细」。
                error_detail=list(errors) if errors else None,
                finished_at=finished_at or datetime.now(),
            )
        )

    async def list_batches(
        self, params: PageParams, *, status: str | None = None
    ) -> tuple[Sequence[Any], int]:
        """批次列表，附带上传人用户名。

        这里是 1:1（一批次一个上传人），所以基类默认的行数计数是对的。
        """
        stmt = (
            select(ImportBatch, User.username)
            .outerjoin(User, User.id == ImportBatch.uploaded_by)
            .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        )
        if status:
            stmt = stmt.where(ImportBatch.status == status)
        return await self.paginate(stmt, params)

    async def get_batch(self, batch_id: int) -> tuple[ImportBatch, str | None] | None:
        stmt = (
            select(ImportBatch, User.username)
            .outerjoin(User, User.id == ImportBatch.uploaded_by)
            .where(ImportBatch.id == batch_id)
        )
        row = (await self.session.execute(stmt)).first()
        return (row[0], row[1]) if row else None

    async def list_order_ids(self, batch_id: int) -> list[int]:
        """该批次导入了哪些订单 —— 后台「点批次 → 跳到订单列表」用。"""
        rows = await self.session.scalars(
            select(Order.id).where(Order.import_batch_id == batch_id).order_by(Order.id)
        )
        return list(rows)
