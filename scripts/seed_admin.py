#!/usr/bin/env python3
"""创建初始管理员账号。

用法：
    .venv 里的 python scripts/seed_admin.py

为什么不写成 `database/seed/01_users.sql`：**SQL 算不出 bcrypt 哈希**。
口令哈希必须由 `core/security.hash_password` 生成（它管着盐和 cost），
把哈希写死在 .sql 里等于把明文口令固化进版本库 —— 那是这个项目最不该有的东西。
所以这条种子走 Python，口令从 `.env` 的 `ADMIN_PASSWORD` 读。

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
    username = settings.admin_username.strip()
    password = settings.admin_password

    if not username:
        print("错误：.env 里的 ADMIN_USERNAME 是空的", file=sys.stderr)
        return 1
    if not password:
        print(
            "错误：.env 里的 ADMIN_PASSWORD 是空的。\n"
            "  这个脚本不会替你编一个口令 —— 一个你不知道的管理员口令\n"
            "  比没有管理员更难排查。请自己填一个再重跑。",
            file=sys.stderr,
        )
        return 1
    if len(password) < 8:
        print(
            f"错误：ADMIN_PASSWORD 只有 {len(password)} 位，至少要 8 位。",
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
                print("（想换口令就先删掉这一行，或直接改数据库）")
                return 0

            user = User(
                username=username,
                password_hash=hash_password(password),
                role="ADMIN",
                is_active=True,
            )
            session.add(user)
            await session.commit()
            print(f"已创建管理员：id={user.id} username={user.username} role={user.role}")
            return 0
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
