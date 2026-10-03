"""AI 分析的门面：把「构造 prompt → 调模型 → 解析 → 收敛」串成一次 `analyze`。

backend 只认这里的两个东西：`OrderAnalysisConfig`（配置靠构造参数传入，
本包不读 .env）和 `OrderAnalyzer`（一个 `analyze(OrderInput) -> AnalysisOutcome`
的 Protocol）。测试塞一个假 analyzer 就能不连网、不连库地跑完整条后端链路。
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ai.classifier.order_classifier import AnalysisResult, classify
from ai.extractor.order_extractor import OrderInput
from ai.extractor.response_parser import ResponseParseError, extract_json_object
from ai.llm.base import ChatModel, TokenUsage
from ai.llm.openai_client import OpenAICompatibleClient
from ai.prompts.order_analysis_v2 import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)

#: 非 JSON 时追加在用户消息**开头**的重试提示（《LLM_Prompt设计》§6）。
_JSON_ONLY_RETRY_PREFIX = (
    "注意：你上一次没有输出合法 JSON。这一次只输出一个 JSON 对象，"
    "不要有任何其它文字。\n\n"
)


@dataclass(frozen=True, slots=True)
class OrderAnalysisConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    result: AnalysisResult
    raw: dict | None
    usage: TokenUsage
    model: str
    prompt_version: str


@runtime_checkable
class OrderAnalyzer(Protocol):
    async def analyze(self, order: OrderInput) -> AnalysisOutcome: ...


class OrderAnalysisClient:
    def __init__(
        self, config: OrderAnalysisConfig, *, model: ChatModel | None = None
    ) -> None:
        self._config = config
        self._model = model or OpenAICompatibleClient(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            timeout=config.timeout,
        )
        #: 外部传进来的 model（测试桩）由调用方负责关闭，我们只关自己建的。
        self._owns_model = model is None

    async def analyze(self, order: OrderInput) -> AnalysisOutcome:
        user = build_user_prompt(order)
        reply = await self._model.complete(
            system=SYSTEM_PROMPT, user=user, json_mode=True
        )
        try:
            raw = extract_json_object(reply.text)
        except ResponseParseError:
            # 追加「只输出 JSON」重试一次；再失败就把 ResponseParseError 抛上去，
            # 由 ai_service 走 §6.2 的保守兜底。
            reply = await self._model.complete(
                system=SYSTEM_PROMPT,
                user=_JSON_ONLY_RETRY_PREFIX + user,
                json_mode=True,
            )
            raw = extract_json_object(reply.text)

        return AnalysisOutcome(
            result=classify(raw),
            raw=raw,
            usage=reply.usage,
            model=reply.model,
            prompt_version=PROMPT_VERSION,
        )

    async def aclose(self) -> None:
        if self._owns_model:
            await self._model.aclose()  # type: ignore[attr-defined]
