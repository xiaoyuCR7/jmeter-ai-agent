"""A report with a small JSON slip is mended, not written again."""
import json

import pytest

from app.services.ai.analyzer import repair_json

GOOD = {'overall_assessment': '下单接口 500 占 7.4%', 'conclusion': '先查下单', 'key_findings': ['a', 'b']}


def test_a_stray_bracket_after_a_string_is_dropped():
    """The slip DeepSeek made: '…改善幅度不能直接量化到产能。"),"timeline_analysis"'."""
    text = json.dumps(GOOD, ensure_ascii=False).replace('"先查下单"', '"先查下单")')
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    assert repair_json(text) == GOOD


def test_a_quote_meant_inside_a_string_is_escaped():
    text = '{"overall_assessment": "断言 "Expected status 201" 失败", "conclusion": "x"}'
    assert repair_json(text) == {'overall_assessment': '断言 "Expected status 201" 失败', 'conclusion': 'x'}


def test_other_damage_is_not_guessed_at():
    with pytest.raises(json.JSONDecodeError):
        repair_json('{"overall_assessment": "cut off')
    assert repair_json(json.dumps(GOOD)) == GOOD


def test_a_wrong_closing_bracket_on_a_list_is_corrected():
    """What the model really wrote: a list of findings closed with ')' instead of ']'."""
    text = '{"key_findings":["a","不能直接量化到产能。"),"timeline_analysis":"0–5s 为爬坡段","conclusion":"x"}'
    assert repair_json(text) == {'key_findings': ['a', '不能直接量化到产能。'], 'timeline_analysis': '0–5s 为爬坡段',
                                 'conclusion': 'x'}
