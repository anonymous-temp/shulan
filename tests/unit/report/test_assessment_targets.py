"""Integrity of the versioned evidence-based target table (assessment_targets.json)."""

import importlib.util
import re
from decimal import Decimal
from pathlib import Path

from modules.report import assessment_clinical_rules as rules

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_LEVELS = {'指南', '行业标准', '专家共识', '科学声明', '系统综述', '人群研究'}


def _builder():
    spec = importlib.util.spec_from_file_location(
        'build_assessment_targets', ROOT / 'scripts' / 'build_assessment_targets.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _numbers(text):
    return {Decimal(token) for token in re.findall(r'(?<![\d.])\d+(?:\.\d+)?(?!\d)(?!\.\d)', text)}


def test_every_target_has_a_verifiable_source_and_quote():
    table = rules.target_table()
    assert table['targets']
    for entry in table['targets']:
        assert entry['indicator'] in table['indicators']
        assert entry.get('evidenceLevel') in EVIDENCE_LEVELS, entry['id']
        assert entry['sources'], entry['id']
        for source in entry['sources']:
            assert source['title'] and int(source['year']) >= 1990
            assert source.get('doi') or str(source.get('url', '')).startswith('http')
        checks = entry['verification'] if isinstance(entry['verification'], list) else [entry['verification']]
        for check in checks:
            quote = check['quote']
            assert quote and len(quote) >= 20
            value = check['sourceValue']
            for bound in (value['low'], value['high']):
                if bound is not None and (entry['low'] is not None or entry['high'] is not None):
                    # The merged entry keeps each quote with its own bound.
                    assert Decimal(str(bound)) in _numbers(quote) or len(checks) > 1, entry['id']


def test_conditions_use_the_fixed_vocabulary():
    table = rules.target_table()
    vocabulary = set(table['conditions'])
    for entry in table['targets']:
        conditions = entry['conditions']
        assert conditions['sex'] in {None, 'male', 'female'}
        assert set(conditions['excludeConditions']) <= vocabulary
        assert set(conditions['requiresConditions']) <= vocabulary
        assert conditions['minAge'] is None or conditions['minAge'] >= 18


def test_sex_specific_targets_cover_both_sexes():
    table = rules.target_table()
    for indicator in {entry['indicator'] for entry in table['targets'] if entry['conditions']['sex']}:
        sexes = {entry['conditions']['sex'] for entry in table['targets'] if entry['indicator'] == indicator}
        assert {'male', 'female'} <= sexes or None in sexes, indicator


def test_reader_text_has_no_internal_wording():
    for entry in rules.target_table()['targets']:
        for text in (entry['text'], entry.get('basis') or '', entry.get('riskTier') or ''):
            assert not re.search(r'模板|原件|原报告|模型', text), entry['id']


def test_sample_report_numbers_are_adopted_only_with_a_source():
    table = rules.target_table()
    ids = {entry['id'] for entry in table['targets']}
    assert table['exampleValues']
    for item in table['exampleValues']:
        assert item['status'] in {'adopted', 'notAdopted'} and item['reason']
        if item['status'] == 'adopted':
            assert item['targetId'] in ids


def test_builder_covers_every_indicator_of_the_sample_chapter_three():
    builder = _builder()
    researched = {candidate['indicator'] for candidate in builder.CANDIDATES}
    required = {
        'homaIr',
        'fastingInsulin',
        'cPeptide',
        'hba1c',
        'fpg',
        'tg',
        'tgHdl',
        'ldlC',
        'apoB',
        'uricAcid',
        'hsCrp',
        'ast',
        'alt',
        'ggt',
        'alp',
        'ferritin',
        'mcv',
        'tsh',
        'hcy',
        'vitaminD',
        'bmi',
        'waist',
        'bodyFat',
        'visceralFat',
        'sbp',
    }
    assert required <= researched
    assert set(rules.target_table()['indicators']) == set(builder.CATALOG)


def test_builder_rejects_quotes_that_do_not_contain_the_bound():
    builder = _builder()
    document = {'title': 'T', 'url': 'https://pubmed.ncbi.nlm.nih.gov/1/', 'text': ''}
    text = 'A true healthy normal ALT level ranges from 29 to 33 IU/l for males, 19 to 25 IU/l for females.'
    proposal = {'quote': text, 'high': 33, 'sourceUnit': 'IU/l'}
    assert builder._verify(proposal, document, text, 'alt') == (True, 'verified')
    assert builder._verify({**proposal, 'high': 35}, document, text, 'alt')[0] is False
    assert builder._verify({**proposal, 'quote': text.replace('33', '34')}, document, text, 'alt')[0] is False
    ratio = {'quote': 'The TG/HDL-C cut-off was 1.7 for women in this cohort study.', 'high': 1.7, 'sourceUnit': ''}
    assert builder._verify(ratio, document, ratio['quote'], 'tgHdl')[0] is False  # the lipid unit must be stated


def test_builder_accepts_only_authoritative_web_hosts():
    builder = _builder()
    assert builder.authoritative('https://pmc.ncbi.nlm.nih.gov/articles/PMC1/')
    assert builder.authoritative('https://diabetesjournals.org/care/article/48/x')
    assert not builder.authoritative('https://www.example-news.cn/health/1.html')
