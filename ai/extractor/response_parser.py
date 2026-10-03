"""从 LLM 返回的文本里硬抠出一个 JSON 对象。

为什么不能直接 `json.loads(text)`：即便要求「只输出 JSON」，模型仍可能包一层
Markdown 代码块围栏，或在前后加一句解释。这里做两件确定性的事：
剥围栏 → 取第一个**平衡**的 `{...}`。不做任何「猜测式修复」——
修坏了比直接报错更难查。
"""

import json
import re

#: 匹配 ```json ... ``` 或 ``` ... ``` 围栏，捕获中间内容。
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ResponseParseError(Exception):
    """返回内容里抠不出合法的 JSON 对象。上层可据此「追加一句只输出 JSON」重试。"""


def extract_json_object(text: str) -> dict:
    if not text or not text.strip():
        raise ResponseParseError("返回内容为空")

    cleaned = text.strip()
    fenced = _FENCE.search(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()

    start = cleaned.find("{")
    if start == -1:
        raise ResponseParseError("未找到 JSON 对象")

    # 扫描到与第一个 '{' 配对的 '}'。要跟踪字符串状态，否则字段值里的
    # '{' '}'（比如 risk_reason 里写了「{未填写}」）会把深度算错。
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = cleaned[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise ResponseParseError(f"JSON 解析失败：{exc}") from exc
                if not isinstance(parsed, dict):
                    raise ResponseParseError("JSON 顶层不是对象")
                return parsed

    raise ResponseParseError("JSON 括号不平衡")
