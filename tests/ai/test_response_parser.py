"""`ai/extractor/response_parser.py` 的单测 —— 纯逻辑，不连库、不连网。

既然 DeepSeek 不支持 `json_schema`（memory `project_deepseek_structured_output`），
从文本里抠 JSON 这一步就是**契约的第一道关**。它的行为必须完全确定：
能剥的围栏剥掉、能认的嵌套认出来，认不出就**明确报错**（好让上层追加一句
「只输出 JSON」重试），绝不「猜着修」。
"""

import json

import pytest

from ai.extractor.response_parser import ResponseParseError, extract_json_object

pytestmark = pytest.mark.unit


# ============================================================
# 正常形态
# ============================================================


def test_parses_a_bare_json_object():
    assert extract_json_object('{"risk_level": "LOW"}') == {"risk_level": "LOW"}


def test_strips_a_json_fence():
    """模型常常把 JSON 包在 ```json 围栏里，即便被明确要求不要。"""
    text = '```json\n{"priority": "HIGH", "deadline": "TODAY"}\n```'

    assert extract_json_object(text) == {"priority": "HIGH", "deadline": "TODAY"}


def test_strips_a_bare_fence():
    text = '```\n{"risk_level": "MEDIUM"}\n```'

    assert extract_json_object(text) == {"risk_level": "MEDIUM"}


def test_ignores_text_before_and_after_the_object():
    """前后各有一句解释 —— 取第一个平衡的 `{...}` 就好，剩下的丢掉。"""
    text = '好的，这是我的判断：{"risk_level": "HIGH"} 希望对你有帮助。'

    assert extract_json_object(text) == {"risk_level": "HIGH"}


def test_parses_nested_objects():
    text = '{"a": {"b": {"c": 1}}, "d": 2}'

    assert extract_json_object(text) == {"a": {"b": {"c": 1}}, "d": 2}


def test_braces_inside_a_string_do_not_confuse_the_depth_scan():
    """`risk_reason` 里写了「{未填写}」时，深度计数不能被字符串里的花括号带偏 ——

    这正是「扫描时必须跟踪字符串状态」那条注释对应的用例。若按朴素的花括号
    计数，会在第一个 `}` 处提前截断，然后 JSON 解析失败。
    """
    text = '{"risk_reason": "地址{未填写}，需确认", "risk_level": "HIGH"}'

    parsed = extract_json_object(text)

    assert parsed["risk_reason"] == "地址{未填写}，需确认"
    assert parsed["risk_level"] == "HIGH"


def test_escaped_quote_inside_a_string_does_not_end_the_string():
    text = '{"action": "联系客户说\\"尽快\\"处理", "risk_level": "LOW"}'

    parsed = extract_json_object(text)

    assert parsed["action"] == '联系客户说"尽快"处理'
    assert parsed["risk_level"] == "LOW"


# ============================================================
# 失败形态 —— 一律明确报错，不猜
# ============================================================


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_text_raises(text):
    with pytest.raises(ResponseParseError):
        extract_json_object(text)


def test_text_without_a_brace_raises():
    with pytest.raises(ResponseParseError):
        extract_json_object("抱歉，我无法完成这个请求。")


def test_unbalanced_braces_raise():
    with pytest.raises(ResponseParseError):
        extract_json_object('{"risk_level": "LOW"')


def test_a_non_object_top_level_raises():
    """顶层是数组/数字/字符串都算非法 —— 契约要的是一个对象。"""
    with pytest.raises(ResponseParseError):
        extract_json_object("[1, 2, 3]")


def test_malformed_json_inside_braces_raises():
    with pytest.raises(ResponseParseError):
        extract_json_object('{"risk_level": LOW}')  # 值没加引号


def test_a_fence_with_prose_but_no_json_raises():
    with pytest.raises(ResponseParseError):
        extract_json_object("```\n这不是 JSON\n```")


def test_the_error_is_our_type_not_a_raw_json_decode_error():
    """上层（`ai.client`）按 `ResponseParseError` 决定「追加只输出 JSON 重试」。
    底层的 `json.JSONDecodeError` 必须被**包住**再抛，否则那条重试分支接不住它，
    一次畸形输出会直接冒到 `ai_service` 的兜底去。"""
    with pytest.raises(ResponseParseError) as exc:
        extract_json_object('{"risk_level": LOW}')

    assert not isinstance(exc.value, json.JSONDecodeError)
    # JSONDecodeError 是 ValueError 的子类；确认我们抛的不是它
    assert not isinstance(exc.value, ValueError)
