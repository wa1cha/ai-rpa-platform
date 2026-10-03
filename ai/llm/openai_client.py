"""OpenAI 兼容接口的异步客户端（DeepSeek / 通义 / Ollama 都吃这套协议）。

为什么 async：`ai_worker` 要用 `asyncio.gather` 并发分析多单，同步客户端会把
并发退化成串行。用 `httpx.AsyncClient` 复用连接池。

结构化输出的**降级梯子只有两档**（实测结论，见 memory `project_deepseek_structured_output`）：
`json_object` → 裸 prompt。`json_schema`（strict 与否）对 `deepseek-chat` 一律
HTTP 400 `This response_format type is unavailable now`，所以不作为梯子的一档。
能力探测结果在**进程内缓存**，避免每次请求都试错。
"""

import asyncio
import logging

import httpx

from ai.llm.base import LLMError, LLMReply, LLMUnavailable, TokenUsage

logger = logging.getLogger(__name__)

#: 可重试失败（429 / 超时 / 5xx）的退避基数。只重试 1 次，所以不需要更复杂的曲线。
_RETRY_BACKOFF_SECONDS = 1.0

#: 判定「供应商不支持 response_format」的启发式关键词。各家的报错措辞不一，
#: 宁可放宽一点：误判的代价只是放弃一个「本就不保证」的强约束，不会出错。
_FORMAT_UNSUPPORTED_MARKERS = (
    "response_format",
    "unavailable",
    "not support",
    "unsupported",
)


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        #: 进程内能力标志：第一次被 400 告知不支持后就永久降级，不再试。
        self._json_mode_supported = True

    async def complete(
        self, *, system: str, user: str, json_mode: bool = False
    ) -> LLMReply:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        json_mode_active = json_mode and self._json_mode_supported
        transient_attempts = 0

        while True:
            try:
                response = await self._post(messages, json_mode_active)
            except httpx.TimeoutException as exc:
                transient_attempts += 1
                if transient_attempts >= 2:
                    raise LLMUnavailable(f"请求超时：{exc}") from exc
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                continue
            except httpx.TransportError as exc:
                transient_attempts += 1
                if transient_attempts >= 2:
                    raise LLMUnavailable(f"连接失败：{exc}") from exc
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                transient_attempts += 1
                if transient_attempts >= 2:
                    raise LLMUnavailable(
                        f"HTTP {response.status_code}：{response.text[:200]}"
                    )
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * transient_attempts)
                continue

            if (
                response.status_code == 400
                and json_mode_active
                and self._looks_format_unsupported(response.text)
            ):
                # 供应商不支持 response_format：永久降级为裸 prompt，重发一次。
                # 这不算「服务不稳定」的重试，所以不占 transient_attempts。
                logger.warning(
                    "供应商不支持 response_format，降级为裸 prompt：%s",
                    response.text[:200],
                )
                self._json_mode_supported = False
                json_mode_active = False
                continue

            if response.status_code >= 400:
                raise LLMError(f"HTTP {response.status_code}：{response.text[:300]}")

            return self._parse(response)

    async def _post(self, messages: list[dict], json_mode: bool) -> httpx.Response:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            # temperature=0：判定要的是稳定，不是创造性。注意即便如此，
            # risk_reason / action 的措辞仍会变 —— 所以评测只比字段决策，不比文本。
            "temperature": 0,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return await self._client.post(self._endpoint, json=payload)

    @staticmethod
    def _looks_format_unsupported(text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in _FORMAT_UNSUPPORTED_MARKERS)

    def _parse(self, response: httpx.Response) -> LLMReply:
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(f"响应不是 JSON：{response.text[:200]}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应结构异常：{response.text[:200]}") from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMError("响应内容为空")

        usage = body.get("usage") or {}
        return LLMReply(
            text=content,
            usage=TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            ),
            model=str(body.get("model") or self.model),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
