"""口令哈希 —— 模拟 ERP 自己的一份实现。

**故意不 import 主服务的 `app.core.security`**，哪怕函数体几乎一样。
模拟 ERP 是外部系统（《模拟ERP设计》§1.1）：它不该知道主服务的存在，
更不该依赖主服务的模块。真 ERP 的密码哈希也必然是自己那一套。

这里只保留 ERP 需要的两个函数。JWT 签发之类的没有 —— ERP 用的是
服务端 session cookie，不是无状态 token。
"""

import bcrypt

#: bcrypt 只处理前 72 字节，超出部分被静默忽略。显式截断，别让这个上限成为隐藏行为。
_BCRYPT_MAX_BYTES = 72

#: 计算强度。与主服务一致：够慢到能挡住离线爆破，又快到登录不卡。
_BCRYPT_ROUNDS = 12


def _to_bcrypt_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    hashed = bcrypt.hashpw(_to_bcrypt_bytes(password), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS))
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码。哈希串损坏时 bcrypt 抛 ValueError，按「校验失败」处理 ——
    认证流程不该因为一条脏数据变 500。
    """
    try:
        return bcrypt.checkpw(_to_bcrypt_bytes(password), password_hash.encode("utf-8"))
    except ValueError:
        return False
