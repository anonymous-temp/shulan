"""Encoding defects must be rejected without corrupting valid medical text."""

import pytest

from agents.llm.text_quality import TextQualityError
from agents.llm.text_quality import check_generated_value
from agents.llm.text_quality import check_text


@pytest.mark.parametrize(
    'bad', ['几\ufffd个月', '\ud800', '腹\x00胀', '餐后\u202e不适', '腹\u200b胀', '\uffff', '锟斤拷', '额ก几个月了']
)
def test_detects_defects_in_nested_json_and_logs_no_text(bad):
    with pytest.raises(TextQualityError) as captured:
        check_generated_value({'private': {'options': ['正常', bad]}}, chinese_context=True)
    assert bad not in str(captured.value)
    assert 'private' not in str(captured.value)


@pytest.mark.parametrize(
    'good',
    [
        'HbA1c 6.1%，β-受体，μmol/L，37.5 ℃，10⁹/L',
        '𠮷先生，罕见字𠀀',
        '正常\n可补充\t说明\r\n🙂👩‍⚕️',
        'Ελληνικά',
        '中文与 English',
    ],
)
def test_preserves_legitimate_unicode(good):
    check_text(good, chinese_context=True)


def test_script_check_is_opt_in_and_supports_literal_source_terms():
    check_text('中文かな')
    check_text('中文かな', chinese_context=True, source_text='材料中的原文：中文かな')
    with pytest.raises(TextQualityError):
        check_text('额ก几个月了', chinese_context=True, source_text='报告：血糖正常')
    with pytest.raises(TextQualityError):
        check_text('\ufffd', source_text='\ufffd')


def test_foreign_source_terms_can_be_acknowledged_with_a_chinese_prefix():
    check_text('您提到Иван先生的报告。', chinese_context=True, source_text='姓名：Иван')
    check_text('您提到中文かな。', chinese_context=True, source_text='原文：中文かな')
    with pytest.raises(TextQualityError):
        check_text('您提到额ก几个月。', chinese_context=True, source_text='姓名：Иван')
    with pytest.raises(TextQualityError):
        check_text('锟斤拷随后烫烫烫', source_text='允许引用原文锟斤拷')
