"""口令哈希与 JWT —— 认证只在这里落地，其它地方不许自己碰密码。

两条边界：
  · 密码**只以 bcrypt 哈希形式**离开这个模块，任何地方都不出现明文
  · JWT 的签发与解析都走这里，`jwt_secret` 只从配置读
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import settings
from app.core.exceptions import AuthError

#: bcrypt 只处理前 72 字节，超出部分会被**静默忽略**。
#: 与其让「超长密码的 73 字节之后无效」变成隐藏行为，不如在这里显式截断，
#: 并让调用方知道这个上限的存在。
_BCRYPT_MAX_BYTES = 72

#: bcrypt 的计算强度。12 在「够慢」和「登录别太卡」之间取平衡；
#: 调高会让每次登录变慢，调低会让离线爆破变便宜。
_BCRYPT_ROUNDS = 12


def _to_bcrypt_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """生成 bcrypt 哈希。

    bcrypt 自带盐（存在哈希串里），所以同一个密码每次哈希结果都不同 ——
    这是对的，不要试图「哈希结果稳定」。
    """
    hashed = bcrypt.hashpw(_to_bcrypt_bytes(password), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS))
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码。

    哈希串损坏时 bcrypt 会抛 ValueError，这里当作「校验失败」处理 ——
    认证流程里不该因为一条脏数据就 500。
    """
    try:
        return bcrypt.checkpw(_to_bcrypt_bytes(password), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(subject: int | str, role: str, **extra: Any) -> str:
    """签发 JWT。

    `role` 放进 payload 是为了让鉴权**不需要查库** —— RPA Worker 跑在另一台机器上，
    每分钟可能领几十次任务，每次都查一次 users 表纯属浪费。

    代价：改了用户角色后，旧 token 里的 role 仍是旧的，要等 token 过期才生效。
    v1 接受；真要即时生效就得引入黑名单，那又把 JWT 无状态的收益吃掉了。
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
        **extra,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """解析并校验 JWT。任何问题都抛 AuthError（→ HTTP 401 / code 4001）。

    过期、签名不对、格式错乱在调用方看来是同一件事：**你得重新登录**。
    所以不区分具体原因，但日志里可以通过异常类型看到细节。
    """
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("身份凭证无效") from exc
