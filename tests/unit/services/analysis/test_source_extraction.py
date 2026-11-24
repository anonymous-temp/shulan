"""Report flag utilities remain separate from LLM JSON extraction."""

import pytest

from utils.report_flags import normalize_report_flag
from utils.report_flags import report_flag_interpretation
from utils.report_flags import report_flag_is_abnormal


@pytest.mark.parametrize(
    'raw, expected',
    [
        ('超标', '偏高'),
        ('缺乏', '偏低'),
        ('正常', '正常'),
        ('H', '偏高'),
        ('↑', '偏高'),
        (' L ', '偏低'),
        ('未见异常', '正常'),
        ('需求高', None),
        ('失衡', None),
        ('异常', None),
        ('风险升高', None),
        ('不正常', None),
        ('正常或偏高', None),
        (None, None),
    ],
)
def test_flag_mapping_is_literal_not_a_substring_or_reference_range_inference(raw, expected):
    assert normalize_report_flag(raw) == expected


def test_unknown_direction_is_abnormal_without_inventing_profile_direction():
    assert report_flag_is_abnormal('失衡')
    assert report_flag_is_abnormal('需求高')
    assert report_flag_interpretation('需求高') is None
    assert report_flag_interpretation('危急值') == 'critical'
    assert not report_flag_is_abnormal('未见异常')
