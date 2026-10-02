#!/usr/bin/env python3
"""创建 RPA Worker 账号。

用法：
    <PYTHON_BIN> scripts/seed_worker.py

和 `seed_admin.py` 是同一套理由：**SQL 算不出 bcrypt 哈希**，所以这条种子
走 Python，口令从 `.env` 的 `WORKER_PASSWORD` 读。

为什么 Worker 要单独一个账号：Worker 跑在另一台机器上，权限必须最小化。
它只需要「领任务 / 心跳 / 回传 / 传图」四件事，`role='WORKER'` 让它连订单
列表都打不开（`/rpa/*` 之外一律 403）。给 Worker 用管理员账号，等于为了
少建一个账号，把整站的管理权限发到了那台机器上。

幂等：账号已存在就什么都不做，不会悄悄改掉现有口令。
"""

import asyncio
import sys
from pathlib import Path

# scripts/ 不在包路径里，直接把 backend/ 挂上去，脚本才能 import app.*
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.database.mysql import AsyncSessionLocal, dispose_engine  # noqa: E402
from app.models.user import User  # noqa: E402


async def main() -> int:
    username = settings.worker_username.strip()
    password = settings.worker_password

    if not username:
        print("错误：.env 里的 WORKER_USERNAME 是空的", file=sys.stderr)
        return 1
    if not password:
        print(
            "错误：.env 里的 WORKER_PASSWORD 是空的。\n"
            "  和 ADMIN_PASSWORD 一样，这个脚本不会替你编一个口令 ——\n"
            "  一个你不知道的 Worker 口令，意味着 Worker 连不上，\n"
            "  而报错会发生在另一台机器上，排查成本高得多。请自己填一个再重跑。",
            file=sys.stderr,
        )
        return 1
    if len(password) < 8:
        print(
            f"错误：WORKER_PASSWORD 只有 {len(password)} 位，至少要 8 位。",
            file=sys.stderr,
        )
        return 1

    try:
        async with AsyncSessionLocal() as session:
            existing = await session.scalar(
                select(User).where(User.username == username)
            )
            if existing is not None:
                print(f"已存在，跳过：id={existing.id} username={existing.username} "
                      f"role={existing.role} is_active={existing.is_active}")
                if existing.role != "WORKER":
                    print(
                        f"  ^ 注意：它的角色是 {existing.role}，不是 WORKER —— "
                        f"/rpa/* 会返回 403。想修就改 users.role，或删掉这行重跑。"
                    )
                return 0

            user = User(
                username=username,
                password_hash=hash_password(password),
                role="WORKER",
                is_active=True,
            )
            session.add(user)
            await session.commit()
            print(f"已创建 RPA Worker：id={user.id} username={user.username} role={user.role}")
            return 0
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
