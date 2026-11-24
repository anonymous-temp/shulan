from tests.unit.gut_barrier.preview.archive_support import historical_asset
"""v5 opts in to cleanup without mutating frozen v4 or public block shapes."""
from hashlib import sha256
from pathlib import Path
from copy import deepcopy

from adapters.gut_barrier.latest_view import arrange_sections
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import (
    CLEAN_REVIEW_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE, REFINED_XU_DISPLAY_TEMPLATE,
    SOURCE_STAGE_DISPLAY_TEMPLATE, display_capabilities, resolve_display_template,
)
from tests.unit.gut_barrier.preview.support import _report


def test_v4_asset_and_known_html_remain_byte_identical():
    asset = historical_asset("v4")
    assert sha256(asset.read_bytes()).hexdigest() == 'd54b46dbd95d6d305aa4d62eb2d1a1ac7e5830e23d6ce68962f45de64851697d'
    old_html = render_report_html(_report(), template_version=REFINED_XU_DISPLAY_TEMPLATE,
                                 template_archive_dir=asset.parent)
    assert sha256(old_html.encode()).hexdigest() == 'd6eb413e68e77993158f146a76a9e106357cdfa5d4035f55a71a808db9f662e2'


def test_v5_explicit_opt_in_keeps_unbound_receipts_on_v1():
    assert LATEST_DISPLAY_TEMPLATE == "gut-barrier-static.v14"
    assert display_capabilities(CLEAN_REVIEW_DISPLAY_TEMPLATE).clean_review_layout
    assert not display_capabilities(REFINED_XU_DISPLAY_TEMPLATE).clean_review_layout
    assert resolve_display_template({'schemaVersion': 'gut-barrier-public.v2'}) == 'gut-barrier-static.v1'


def test_source_scope_is_shown_before_assessment_without_mutating_report():
    sections = [{'id': 'overview', 'blocks': [
        {'kind': 'table', 'title': '1.1 检测综合评估结论'},
        {'kind': 'paragraph', 'title': '本次评估资料范围', 'text': '身份不符的附件未采用。'},
        {'kind': 'facts', 'title': '受检者信息'},
    ]}]
    before = deepcopy(sections)
    result = arrange_sections(sections, clean_review_layout=True)
    assert [b['title'] for b in result[0]['blocks']] == [
        '受检者信息', '本次评估资料范围', '1.1 检测综合评估结论']
    assert sections == before
