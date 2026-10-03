"""LLM 客户端的抽象 —— 让上层不依赖任何具体供应商。

`ChatModel` 是一个 Protocol（结构化类型），不是 ABC：这样测试里塞一个
「有 `complete` 方法的普通对象」就能当桩用，不必继承任何基类。
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LLMError(Exception):
    """LLM 调用失败的基类。上层（`ai.client`）据此决定是否重试。"""


class LLMUnavailable(LLMError):
    """连不上 / 超时 / 5xx —— 可重试的失败。

    与「返回了内容但不合法」区分开：那种是 `ResponseParseError`，
    重试的是「换一种更严格的提示」，而不是「再等等网络」。
    """


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """token 用量。评测报告要按它算钱。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class LLMReply:
    text: str
    usage: TokenUsage
    model: str


@runtime_checkable
class ChatModel(Protocol):
    """一次补全。`json_mode=True` 时请求供应商的结构化输出模式。"""

    async def complete(
        self, *, system: str, user: str, json_mode: bool = False
    ) -> LLMReply: ...
