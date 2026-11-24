from types import SimpleNamespace

from adapters.report.web_evidence import WEB_PROVIDER
from adapters.report.web_evidence import web_original_key
from modules.report.evaluation_evidence_recall import RecallLimits
from modules.report.evaluation_evidence_recall import _match
from modules.report.evaluation_evidence_recall import _prepare_topics
from modules.report.evaluation_evidence_recall import _queries
from modules.report.evaluation_evidence_recall import _select
from modules.report.evaluation_evidence_recall import _term_in
from modules.report.schemas import EvidenceRecord


def _record(key, title, text, *, category='literature', year=2024, study_types=()):
    return EvidenceRecord(
        evidence_key=key,
        source_evidence_key=web_original_key(text),
        provider=WEB_PROVIDER,
        source_id=f'https://www.nature.com/articles/{key}',
        category=category,
        title=title,
        source='Journal',
        year=year,
        evidence_text=text,
        study_types=list(study_types),
        url=f'https://www.nature.com/articles/{key}',
    )


def _topic(topic_id, kind, title, aliases, **extra):
    return {
        'topicId': topic_id,
        'kind': kind,
        'title': title,
        'aliases': list(aliases),
        'sourceFactIds': ['f1'],
        'chapterCodes': ['microbiomeAssessment'],
        'targetIds': [topic_id + '-cell'],
        'allowedRoles': ['interpretation'],
        **extra,
    }


def test_object_terms_match_as_phrases_or_as_words_of_one_sentence():
    sentence = 'The ratio of Firmicutes to Bacteroidetes was higher in obese adults.'
    assert _term_in(sentence, 'Firmicutes/Bacteroidetes')
    assert _term_in(sentence, 'Firmicutes Bacteroidetes ratio')
    assert not _term_in(
        'Firmicutes were abundant. Bacteroidetes ratio fell.'.split('. ')[0], 'Firmicutes Bacteroidetes ratio'
    )
    assert _term_in('粪便隐血阳性者应接受结肠镜检查。', '粪便隐血')
    # A taxon or gene is matched exactly, never by scattered words.
    assert _term_in('Eggerthella lenta was enriched.', 'Eggerthella lenta', exact=True)
    assert not _term_in('Eggerthella species and Gordonibacter lenta-like strains.', 'Eggerthella lenta', exact=True)


def test_a_publication_is_linked_only_when_its_text_names_the_object():
    flora = _topic(
        't-egg',
        'abnormal_flora',
        'Eggerthella lenta',
        ['迟缓埃格特菌', 'Eggerthella lenta'],
        objectTerms=['Eggerthella lenta'],
    )
    on_topic = [
        _record(
            'a',
            'Eggerthella lenta: metabolism and pathogenesis',
            'Eggerthella lenta is a gut actinobacterium. Eggerthella lenta inactivates digoxin in humans.',
        )
    ]
    off_topic = [_record('b', 'Gut actinobacteria in health', 'Bifidobacterium dominates the infant gut. ' * 5)]
    case = [_record('c', 'Eggerthella lenta bacteremia: a case report', 'Eggerthella lenta was isolated from blood.')]
    edge = _match(on_topic, flora)
    assert edge['matchBasis'] == 'literal_title_and_object' and edge['selectedQuotes']
    assert _match(off_topic, flora) is None
    assert _match(case, flora) is None

    clinical = _topic(
        't-fobt',
        'clinical',
        '粪便隐血试验弱阳性（反复）',
        ['粪便隐血试验弱阳性（反复）'],
        objectTerms=['fecal occult blood', '粪便隐血'],
    )
    passing = [
        _record(
            'd', 'Dietary fibre and health', 'Fecal occult blood testing was not part of this trial. ' + 'Fibre. ' * 20
        )
    ]
    assert _match(passing, clinical) is None  # mentioned once in passing, not a document about the problem
    guideline = [
        _record('e', '结直肠癌筛查与早诊早治方案', '粪便隐血阳性者应在规定时间内完成结肠镜检查。', category='guideline')
    ]
    assert _match(guideline, clinical)['score'] > 3


def test_selection_gives_every_topic_its_best_source_first():
    topics = [
        _topic('t1', 'clinical', '脂肪肝', ['脂肪肝'], objectTerms=['fatty liver', 'MASLD']),
        _topic('t2', 'abnormal_flora', 'Eggerthella lenta', ['Eggerthella lenta'], objectTerms=['Eggerthella lenta']),
    ]
    body = 'MASLD management requires weight loss. Patients with MASLD should be assessed with FIB-4. MASLD is common.'
    pool = [
        _record('g1', 'Guidelines on the management of MASLD', body, category='guideline', year=2024),
        _record('g2', 'MASLD in adults: a narrative review', body, year=2020, study_types=['Review']),
        _record('g3', 'MASLD cohort study', body, year=2019),
        _record('g4', 'MASLD and diet', body, year=2018),
        _record(
            'x1', 'Eggerthella lenta in the gut', 'Eggerthella lenta metabolises drugs. Eggerthella lenta is common.'
        ),
    ]
    chosen, links = _select(
        pool,
        topics,
        subject=SimpleNamespace(age=48, sex='男'),
        facts=[],
        limits=RecallLimits(max_publications=3, clinical_per_topic=3, default_per_topic=2),
    )
    titles = {links[identity][0]['topicId'] for identity in chosen}
    assert len(chosen) == 3 and titles == {'t1', 't2'}  # the taxon keeps its only source despite the cap


def test_duplicate_taxon_topics_merge_and_each_structure_index_gets_its_own_question():
    blueprint = SimpleNamespace(
        chapters=[
            {
                'code': 'microbiomeAssessment',
                'targets': [
                    {
                        'id': 'cell-gmwi2',
                        'owner': {
                            'code': 'gmwi2',
                            'name': '肠道微生物组健康指数（GMWI2）',
                            'primaryFactIds': ['f-gmwi2'],
                        },
                    },
                    {
                        'id': 'cell-fb',
                        'owner': {'code': 'fbRatio', 'name': '厚壁菌门/拟杆菌门（F/B）', 'primaryFactIds': ['f-fb']},
                    },
                    {'id': 'cell-sum', 'owner': {'code': 'microbiomeAssessment', 'name': '总评', 'aggregate': True}},
                ],
            }
        ]
    )
    plan = [
        _topic('latin', 'abnormal_flora', 'Lacticaseibacillus casei', ['干酪乳酪杆菌', 'Lacticaseibacillus casei']),
        {**_topic('zh', 'abnormal_flora', '干酪乳酪杆菌', ['干酪乳酪杆菌']), 'targetIds': ['cell-taxon']},
        {
            **_topic('structure', 'microbiome', '肠道菌群种类', ['gut microbiome diversity']),
            'targetIds': ['cell-gmwi2', 'cell-fb', 'cell-sum'],
        },
    ]
    topics = _prepare_topics(plan, blueprint)
    flora = [topic for topic in topics if topic['kind'] == 'abnormal_flora']
    assert len(flora) == 1 and set(flora[0]['targetIds']) == {'latin-cell', 'cell-taxon'}
    assert flora[0]['objectTerms'] == ['Lacticaseibacillus casei']
    structure = {topic.get('structureCode'): topic for topic in topics if topic['kind'] == 'microbiome'}
    assert set(structure) == {'gmwi2', 'fbRatio'}
    assert structure['gmwi2']['targetIds'] == ['cell-gmwi2'] and 'GMWI2' in structure['gmwi2']['objectTerms']
    assert _queries(structure['fbRatio'])['en'][0].startswith('Firmicutes Bacteroidetes ratio')
    assert _queries(flora[0])['en'][0].startswith('"Lacticaseibacillus casei"')


def test_planned_bilingual_queries_replace_the_group_label():
    topic = _topic(
        't',
        'clinical',
        '糖代谢风险（空腹C肽偏高）',
        ['糖代谢风险（空腹C肽偏高）'],
        plannedQueries={'zh': ['糖尿病前期 筛查 干预 共识'], 'en': ['prediabetes screening management guideline']},
    )
    assert _queries(topic) == {
        'zh': ['糖尿病前期 筛查 干预 共识'],
        'en': ['prediabetes screening management guideline'],
        'pubmed': ['prediabetes screening management guideline'],
    }
    fallback = _queries(_topic('t', 'clinical', '胆囊息肉（约0.4cm）', ['胆囊息肉（约0.4cm）']))
    assert fallback['zh'] == ['胆囊息肉 指南'] and fallback['en'] == []


def test_sources_written_for_older_people_do_not_govern_a_middle_aged_reader():
    from types import SimpleNamespace

    from modules.report.evaluation_report_evidence import _subject_scope_matches

    middle_aged = SimpleNamespace(age=48, sex='男')
    older = SimpleNamespace(age=72, sex='男')
    for title in (
        'Expert Consensus on the Management of Obesity in Older Adults (2026 Edition)',
        '老年肥胖症管理专家共识',
    ):
        assert _subject_scope_matches(title, middle_aged) is False
        assert _subject_scope_matches(title, older) is True
    assert _subject_scope_matches('Obesity in middle-aged and older adults: a cohort', middle_aged) is True
    assert _subject_scope_matches('成人肥胖食养指南（2024年版）', middle_aged) is True


def test_other_patient_populations_and_veterinary_hosts_are_not_evidence_for_this_reader():
    from types import SimpleNamespace

    from modules.report.evaluation_report_evidence import _subject_scope_matches

    reader = SimpleNamespace(age=48, sex='男')
    for title in (
        'Age-specific changes in gut microbial diversity in cystic fibrosis: a meta-analysis',
        'Lower gut dysbiosis and mortality in acute critical illness',
        'Transcranial Doppler after traumatic brain injury',
        'Aeromonas veronii tolC modulates the immune response of freshwater pearl mussels',
    ):
        assert _subject_scope_matches(title.casefold(), reader) is False, title
    for title in (
        'Fecal microbiota transplantation for constipation: a randomized trial',
        'Fish oil supplementation and triglycerides',
        'Butyrate-producing bacteria in mice fed a high-fat diet',
    ):
        assert _subject_scope_matches(title.casefold(), reader) is True, title


def test_near_identical_titles_are_one_publication():
    from modules.report.evaluation_evidence_recall import _same_title
    from modules.report.evaluation_evidence_recall import _title_key

    first = _title_key('Guideline for diagnosis and management of MAFLD-associated fatty liver disease in primary care')
    second = _title_key(
        'Guidelines for Diagnosis and Management of MAFLD-associated Fatty Liver Disease in Primary Care'
    )
    other = _title_key('Guidelines for diagnosis and management of steatotic liver disease in older adults')
    assert _same_title(first, second) and not _same_title(first, other)


def test_veterinary_hosts_are_excluded_even_for_gene_topics():
    from modules.report.evaluation_report_evidence import veterinary_host

    assert veterinary_host('Aeromonas veronii tolC modulates the immune response of freshwater pearl mussels')
    assert not veterinary_host('Toxin genes of Clostridioides difficile isolated from adults')
