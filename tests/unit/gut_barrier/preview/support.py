"""Shared preview helpers; business fixtures remain renderer-independent."""
from html.parser import HTMLParser
import re
from tests.unit.gut_barrier.report_fixtures import *

def visible_source(html):
    return re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.S)

class Parsed(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

def figure_source(body, number):
    return body.split(f'id="figure-{number}"', 1)[1].split("</figure>", 1)[0]

def _report():
    report = reordered_chapter_fixture()
    overview = report["sections"][0]["blocks"]
    overview.extend([
        {"kind": "facts", "title": "受检者信息", "items": [
            {"label": "姓名", "value": "模拟受检者甲"}, {"label": "性别", "value": "女"},
            {"label": "年龄", "value": "50 岁"}, {"label": "检测日期", "value": None},
            {"label": "报告日期", "value": "2026-09-24"}]},
        {"kind": "table", "title": "本次检测方法与参考信息", "columns": ["检测指标", "本次检测方法", "实验室原始参考信息"],
         "rows": [["D-乳酸", None, []], ["DAO", "", None], ["LPS", None, None], ["hs-CRP", None, None]]},
        {"kind": "table", "title": "本次样本与质控记录", "columns": ["校验项目", "记录"],
         "rows": [["样本类型", None], ["样本质量", None], ["检测人", None]]},
        {"kind": "bands", "title": "屏障评分分级", "items": [
            {"range": span, "label": label, "selected": False}
            for span, label in [("1–2分", "I 级（亚健康）"), ("＞2–4.5分", "II 级（轻度）"),
                                ("＞4.5–6分", "III 级（中度）"), ("＞6–8分", "IV 级（重度）"), ("＞8分", "V 级（极重度）")]]},
        {"kind": "bands", "title": "hs-CRP 项目功能分层", "items": [
            {"range": span, "label": label, "selected": False}
            for span, label in [("≤0.5 mg/L", "理想范围"), ("0.5–1 mg/L", "轻度升高"),
                                ("1–3 mg/L", "中度升高"), ("＞3 mg/L", "显著升高")]]},
    ])
    appendix = report["sections"][-1]["blocks"]
    appendix[-1]["items"] = [{"citation": f"原文献{index}", "url": None} for index in range(1, 15)]
    appendix.append({"kind": "references", "title": "营养与微生态干预补充依据", "items": [
        {"citation": "补充文献一", "url": "https://example.org/one"}, {"title": "历史标题兼容", "link": "https://example.org/two"}]})
    return report
