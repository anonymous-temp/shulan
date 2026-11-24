import asyncio
from types import SimpleNamespace

from modules.report.evaluation_report import apply_reviewed_report_blocks
from modules.report.evaluation_report_writer import _batches
from modules.report.evaluation_report_writer import _Dossier
from modules.report.evaluation_report_writer import _Library
from modules.report.evaluation_report_writer import _passages
from modules.report.evaluation_report_writer import _spec
from modules.report.evaluation_report_writer import author_report_chapters
from modules.report.evaluation_report_writer import mirror_duplicate_taxa
from modules.report.evaluation_schema import AssessmentScore
from modules.report.evaluation_schema import EvaluationData
from modules.report.evaluation_schema import TaxonAssessment

SOURCE = (
    'Background on screening programmes in many countries.\n\n'
    'Adults with a positive stool-based test should undergo follow-up colonoscopy within nine months. '
    'Longer delays were associated with a higher risk of colorectal cancer.\n\n'
    'Funding statement and acknowledgements of the working group.'
)


def _target(
    target_id,
    path,
    *,
    owner=None,
    allowed=('observation', 'interpretation', 'action'),
    duty='assessment',
    contract=None,
):
    return {
        'id': target_id,
        'path': path,
        'owner': {'name': '粪便隐血', 'primaryFactIds': ['fact-fobt'], **(owner or {})},
        'sourceFactIds': ['fact-fobt', 'fact-tg'],
        'required': True,
        'allowedRoles': list(allowed),
        'contentDuty': duty,
        'cellContract': contract or {'label': '解读', 'question': '?'},
    }


def _blueprint(targets):
    facts = [
        {
            'factId': 'fact-fobt',
            'kind': 'clinical',
            'name': '粪便隐血试验',
            'value': '弱阳性',
            'referenceRange': '阴性',
        },
        {
            'factId': 'fact-tg',
            'kind': 'clinical',
            'name': '甘油三酯(TG)',
            'value': '3.34',
            'unit': 'mmol/L',
            'referenceRange': '0.00-1.70',
        },
    ]
    return SimpleNamespace(
        data=EvaluationData(),
        facts=facts,
        focus=[],
        chapters=[{'code': 'medicalAssessment', 'title': '医学评估', 'number': 2, 'purpose': '', 'targets': targets}],
    )


def _evidence(target_id):
    return SimpleNamespace(
        plan=[],
        references=[SimpleNamespace(number=1, study_types=['Practice Guideline'], source='J')],
        summaries=[
            {
                'number': 1,
                'sourceText': SOURCE,
                'title': 'Follow-up after positive stool tests',
                'year': 2024,
                'category': 'guideline',
                'topicLinks': [
                    {
                        'topicId': 't',
                        'targetIds': [target_id],
                        'score': 4,
                        'objectName': '粪便隐血',
                        'objectTerms': ['stool-based test', 'colonoscopy'],
                    }
                ],
            }
        ],
    )


class FakeModel:
    """Scripted writer and reviewer; records every payload."""

    def __init__(self, reviews):
        self.reviews, self.calls = list(reviews), []

    async def generate(self, payload):
        self.calls.append(payload)
        if payload['task'] == 'write_report_chapter':
            findings = {row['id'] for row in payload.get('reviewFindings', [])}
            return {
                'cells': [
                    {
                        'id': cell['id'],
                        'paragraphs': [
                            {
                                'text': ('改写后：' if cell['id'] in findings else '')
                                + '粪便隐血弱阳性属于筛查阳性，需要查明出血来源；'
                                '指南建议阳性者在九个月内完成结肠镜检查，延迟会增加风险。' * 2,
                                'factIds': ['F001', 'F999'],
                                'cite': ['E1.1', 'E9.9'],
                            }
                        ],
                    }
                    for cell in payload['cells']
                ]
            }
        return self.reviews.pop(0)(payload)


def test_cell_job_descriptions_have_ranges_and_choice_cells_keep_their_vocabulary():
    explanation = _spec(_target('a', ['medical_assessment', 'findings', 0, 'explanation']))
    secondary = _spec(
        _target('b', ['medical_assessment', 'findings', 0, 'explanation'], owner={'clinicalRole': 'secondary_finding'})
    )
    taxon = _spec(_target('c', ['microbiome_assessment', 'taxa', 3, 'explanation']))
    choice = _spec(
        _target(
            'd',
            ['medical_assessment', 'trigger_analysis', 0, 'evidence_strength'],
            contract={'label': '证据强度', 'allowedValues': ['强', '中', '弱']},
        )
    )
    # Cells are written in full: the report keeps its depth while charts carry the raw numbers.
    assert explanation['form'] == 'paragraph' and explanation['min'] >= 150 and explanation['max'] >= 350
    assert secondary['max'] < explanation['max']
    assert taxon['label'] == '菌种评估' and taxon['min'] >= 90
    # Long cells are itemised; short ones (a taxon, a secondary finding) stay one paragraph.
    assert explanation['outline'][0] == '性质与程度' and 'outline' not in taxon and 'outline' not in secondary
    assert _spec(_target('x', ['health_overview', 'overall_summary', 'text']))['max'] >= 800
    assert choice == {**choice, 'form': 'choice', 'allowed': ['强', '中', '弱']}


def test_passages_are_exact_substrings_that_name_the_object():
    passages = _passages(SOURCE, ['colonoscopy'])
    assert passages and all(passage in SOURCE for passage in passages)
    assert all('colonoscopy' in passage for passage in passages)
    assert _passages(SOURCE, ['Eggerthella lenta']) == []


def test_reviewed_text_is_published_with_bound_facts_and_literal_citations():
    target = _target('report:t1', ['medical_assessment', 'trigger_conclusion', 'text'])
    blueprint = _blueprint([target])
    model = FakeModel([lambda payload: {'reviews': [{'id': 'C01', 'verdict': 'pass', 'issues': []}]}])
    audit = {}
    blocks = asyncio.run(
        author_report_chapters(
            model,
            blueprint=blueprint,
            evidence=_evidence('report:t1'),
            subject=SimpleNamespace(name='x', sex='男', age=48),
            deadline=None,
            audit=audit,
        )
    )
    block = blocks['medicalAssessment'][0]
    sentence = block['sentences'][0]
    assert sentence['factIds'] == ['fact-fobt']  # unknown handle F999 is dropped
    assert sentence['evidence'][0]['number'] == 1 and sentence['evidence'][0]['quotes'][0] in SOURCE
    assert sentence['role'] == 'interpretation' and audit['writerUnresolved'] == []
    writer_payload = model.calls[0]
    assert writer_payload['patient']['clinicalRecords'][0]['id'] == 'F001'
    assert writer_payload['evidence'][0]['passages'][0]['pid'] == 'E1.1'
    accepted, rejected = apply_reviewed_report_blocks(blueprint, _evidence('report:t1'), blocks)
    assert rejected == [] and accepted[0]['evidenceRefs'] == [1]
    assert blueprint.data.medical_assessment.trigger_conclusion.text.startswith('粪便隐血弱阳性')
    assert blueprint.data.medical_assessment.trigger_conclusion.evidence_refs == [1]


def test_reviewer_corrections_replace_the_draft_and_serious_findings_trigger_one_rewrite():
    # Two cells of one table are written and reviewed in the same batch.
    targets = [
        _target('report:t1', ['medical_assessment', 'findings', 0, 'explanation']),
        _target('report:t2', ['medical_assessment', 'findings', 1, 'explanation']),
    ]

    def first(payload):
        assert payload['task'] == 'review_report_chapter' and len(payload['cells']) == 2
        return {
            'reviews': [
                {
                    'id': 'C01',
                    'verdict': 'revise',
                    'issues': [{'type': 'fact_error', 'detail': '数值不符'}],
                    'revisedParagraphs': [
                        {
                            'text': '审稿改正：'
                            + '粪便隐血弱阳性需要到消化内科查明出血来源，并按医生安排完成结肠镜检查。' * 3,
                            'factIds': ['F001'],
                            'cite': [],
                        }
                    ],
                },
                {
                    'id': 'C02',
                    'verdict': 'revise',
                    'issues': [{'type': 'unsupported_claim', 'detail': '把筛查阳性写成确诊'}],
                },
            ]
        }

    def second(payload):
        assert [cell['id'] for cell in payload['cells']] == ['C02']
        return {'reviews': [{'id': 'C02', 'verdict': 'pass', 'issues': []}]}

    model = FakeModel([first, second])
    audit = {}
    blueprint = _blueprint(targets)
    blocks = asyncio.run(
        author_report_chapters(
            model, blueprint=blueprint, evidence=_evidence('report:t1'), subject=None, deadline=None, audit=audit
        )
    )
    texts = {block['targetId']: block['sentences'][0]['text'] for block in blocks['medicalAssessment']}
    assert texts['report:t1'].startswith('审稿改正：') and texts['report:t2'].startswith('改写后：')
    rewrite = [call for call in model.calls if call['task'] == 'write_report_chapter'][1]
    assert rewrite['reviewFindings'] == [
        {'id': 'C02', 'issues': [{'type': 'unsupported_claim', 'detail': '把筛查阳性写成确诊'}]}
    ]
    assert audit['writerUnresolved'] == []


def test_unreviewed_or_uncorrectable_prose_is_never_published():
    target = _target('report:t1', ['medical_assessment', 'trigger_conclusion', 'text'])

    def reject(payload):
        return {'reviews': [{'id': 'C01', 'verdict': 'revise', 'issues': [{'type': 'unsafe_advice', 'detail': 'x'}]}]}

    audit = {}
    blocks = asyncio.run(
        author_report_chapters(
            FakeModel([reject, reject]),
            blueprint=_blueprint([target]),
            evidence=_evidence('report:t1'),
            subject=None,
            deadline=None,
            audit=audit,
        )
    )
    assert blocks.get('medicalAssessment', []) == []
    assert audit['writerUnresolved'][0]['reason'] == 'withheld'

    def broken(payload):
        raise RuntimeError('review service down')

    audit = {}
    blocks = asyncio.run(
        author_report_chapters(
            FakeModel([broken, broken]),
            blueprint=_blueprint([target]),
            evidence=_evidence('report:t1'),
            subject=None,
            deadline=None,
            audit=audit,
        )
    )
    assert blocks.get('medicalAssessment', []) == [] and audit['writerUnresolved']


def test_one_taxon_interpretation_is_mirrored_to_its_other_panel_rows():
    data = EvaluationData()
    data.microbiome_assessment.taxa = [
        TaxonAssessment(
            name='普拉梭菌',
            scientific_name='Faecalibacterium prausnitzii',
            value='12.6383',
            explanation='解读',
            evidence_refs=[3],
        ),
        TaxonAssessment(name='普拉梭菌', scientific_name='Faecalibacterium prausnitzii', value='12.6383'),
        TaxonAssessment(name='普拉梭菌', scientific_name='Faecalibacterium prausnitzii', value='9.1'),
        TaxonAssessment(name='拟杆菌属', scientific_name='Bacteroides', value='18.1'),
    ]
    assert mirror_duplicate_taxa(data) == 1
    taxa = data.microbiome_assessment.taxa
    assert (taxa[1].explanation, taxa[1].evidence_refs) == ('解读', [3])
    assert taxa[2].explanation is None and taxa[3].explanation is None


def test_the_record_quota_is_shared_between_the_cells_of_one_batch():
    facts = [{'factId': f'taxon-{i}', 'kind': 'microbiome', 'name': f'菌{i}', 'value': '0.1'} for i in range(300)]
    facts += [
        {'factId': 'score-normal', 'kind': 'microbiome', 'name': '维生素B12合成', 'value': '80', 'status': '正常'},
        {'factId': 'score-low', 'kind': 'microbiome', 'name': '短链脂肪酸合成', 'value': '12', 'status': '偏低'},
    ]
    blueprint = SimpleNamespace(data=EvaluationData(), facts=facts)
    dossier = _Dossier(blueprint, None)
    view = dossier.view([[f'taxon-{i}' for i in range(300)], ['score-normal', 'score-low']], limit=40)
    names = [row['name'] for row in view['cellRecords']]
    # The second cell is not crowded out by the first one's 300 rows, and its
    # deviating row comes before its unremarkable one.
    assert len(names) == 40 and '短链脂肪酸合成' in names[:2] and '维生素B12合成' in names[:4]


def test_summary_review_sees_the_reviewed_text_it_summarises_and_the_score_cell_gets_the_radar():
    detail = _target('report:detail', ['medical_assessment', 'findings', 0, 'explanation'])
    score = _target(
        'report:score',
        ['health_overview', 'overall_score', 'explanation'],
        owner={'aggregate': True, 'name': '七维评分'},
    )
    blueprint = _blueprint([score])
    blueprint.chapters = [{'code': 'healthOverview', 'title': '总览', 'number': 1, 'purpose': '', 'targets': [score]}]
    blueprint.data.health_overview.assessment_chart = [
        AssessmentScore(
            code='metabolic',
            name='代谢相关疾病',
            score=53,
            assessment_level='重点关注',
            explanation='扣分构成：主导问题30分',
        ),
        AssessmentScore(code='aging', name='衰老相关疾病', score=None),
    ]
    previous = {
        'medicalAssessment': [
            {
                'targetId': 'report:detail',
                'sentences': [{'text': '短链脂肪酸合成评分12分，低于参考下限30分，需要关注。'}],
            }
        ]
    }
    seen = {}

    def review(payload):
        seen.update(payload)
        return {'reviews': [{'id': 'C01', 'verdict': 'pass', 'issues': []}]}

    model = FakeModel([review])
    asyncio.run(
        author_report_chapters(
            model,
            blueprint=blueprint,
            evidence=_evidence('report:score'),
            subject=None,
            deadline=None,
            previous_reviewed=previous,
            context_targets={'report:detail': detail},
        )
    )
    writer = model.calls[0]
    assert writer['cells'][0]['row'] == {
        'dimensions': [{'name': '代谢相关疾病', 'score': 53, 'level': '重点关注', 'basis': '扣分构成：主导问题30分'}]
    }
    assert seen['previouslyWritten'] and '短链脂肪酸' in seen['previouslyWritten'][0]['text']


def test_a_correction_of_one_paragraph_does_not_drop_the_others():
    from modules.report.evaluation_report_writer import _merge_revision

    draft = [
        {'text': '第一段：粪便隐血弱阳性属于筛查阳性，需要复查并查明原因。' * 4, 'factIds': [], 'cite': []},
        {'text': '第二段：甘油三酯3.34 mmol/L，约为参考上限的2倍，需要减重。' * 4, 'factIds': [], 'cite': []},
        {'text': '第三段：菌群多样性在参考范围内，整体指数偏低，需要结合饮食解读。' * 4, 'factIds': [], 'cite': []},
    ]
    corrected = {
        'text': '第二段：甘油三酯3.34 mmol/L，约为参考上限1.70的2倍，是否用药由专科评估。' * 4,
        'factIds': [],
        'cite': [],
    }
    merged = _merge_revision(draft, [corrected])
    assert [row['text'][:3] for row in merged] == ['第一段', '第二段', '第三段'] and merged[1] is corrected
    # A full-length rewrite in fewer paragraphs is accepted as it is.
    condensed = [{'text': ''.join(row['text'] for row in draft), 'factIds': [], 'cite': []}]
    assert _merge_revision(draft, condensed) == condensed
    # A short correction that matches no draft paragraph is not guessed at.
    assert _merge_revision(draft, [{'text': '完全不同的内容。' * 5, 'factIds': [], 'cite': []}]) == []


def test_instruction_labels_do_not_lead_a_published_paragraph():
    from modules.report.evaluation_report_writer import _paragraphs

    rows = _paragraphs(
        {'paragraphs': [{'text': '一句话总判断：这次体检的主线是代谢问题。'}, {'text': '②其次是隐血复查。'}]}
    )
    assert [row['text'] for row in rows] == ['这次体检的主线是代谢问题。', '其次是隐血复查。']


def test_reader_wording_and_style_rewrites_keep_every_fact():
    from modules.report.evaluation_report_writer import _faithful_rewrite
    from modules.report.evaluation_report_writer import _paragraphs

    rows = _paragraphs(
        {
            'paragraphs': [
                {
                    'text': '值得注意的是，检测方模型提示肠易激综合征（关联提示）——相关菌偏低。'
                    '您自述留取标本时有痔疮发作，这两项都是本人报告，未经检查确认。按原报告建议6个月后复查。'
                }
            ]
        }
    )
    text = rows[0]['text']
    for banned in ('检测方', '原报告', '未经检查确认', '本人报告', '——', '值得注意'):
        assert banned not in text, banned
    assert '6个月后复查' in text and '痔疮发作' in text
    original = '甘油三酯3.34 mmol/L，约为上限1.70的2倍，可能与夜宵有关。'
    assert _faithful_rewrite(original, '甘油三酯3.34 mmol/L，约是上限1.70的2倍，可能和夜宵有关。')
    assert not _faithful_rewrite(original, '甘油三酯3.34 mmol/L，是上限的2倍，与夜宵有关。')  # lost 1.70 and 可能
    assert not _faithful_rewrite(original, '甘油三酯3.34 mmol/L——约为上限1.70的2倍，可能与夜宵有关。')  # new tell


def test_leaked_internal_handles_are_removed_not_fatal():
    from modules.report.evaluation_report_writer import _check
    from modules.report.evaluation_report_writer import _paragraphs

    rows = _paragraphs({'paragraphs': [{'text': '该菌在研究中与炎症相关（E12.1、E3.2），本次记录F012显示偏高，' * 3}]})
    assert 'E12' not in rows[0]['text'] and 'F012' not in rows[0]['text']
    assert _check({'form': 'paragraph', 'min': 20, 'max': 300}, rows) is None


def test_itemised_cells_keep_their_titles_and_a_block_of_prose_is_sent_back_once():
    from modules.report.evaluation_report_writer import _batches
    from modules.report.evaluation_report_writer import _check
    from modules.report.evaluation_report_writer import _faithful_rewrite
    from modules.report.evaluation_report_writer import _paragraphs

    rows = _paragraphs(
        {
            'paragraphs': [
                {'title': '性质与程度', 'text': '粪便隐血试验弱阳性，属筛查阳性。', 'factIds': ['F001'], 'cite': []},
                {'title': '相关结果：', 'text': '相关结果：同次粪便红细胞0~1/HP。', 'factIds': [], 'cite': []},
                {'title': '', 'text': '您近期没有便血。', 'factIds': [], 'cite': []},
            ]
        }
    )
    assert [row['text'] for row in rows] == [
        '性质与程度：粪便隐血试验弱阳性，属筛查阳性。',
        '相关结果：同次粪便红细胞0~1/HP。',
        '近期没有便血。',
    ]
    spec = _spec(_target('a', ['medical_assessment', 'findings', 0, 'explanation']))
    block = [{'text': '粪便隐血试验弱阳性。' * 30, 'factIds': [], 'cite': []}]
    items = [{'text': f'条目{index}：' + '粪便隐血试验弱阳性。' * 6, 'factIds': [], 'cite': []} for index in range(4)]
    # The first draft must be itemised; a second attempt is published as written.
    assert _check(spec, block, strict=True) == 'not_itemized' and _check(spec, block) is None
    assert _check(spec, items, strict=True) is None
    # A cell that overshoots its range is asked for again once, then published; only a runaway text is refused.
    long = [{'text': f'条目{index}：' + '粪便隐血试验弱阳性。' * 8, 'factIds': [], 'cite': []} for index in range(8)]
    assert _check(spec, long, strict=True) == 'too_long' and _check(spec, long) is None
    assert _check(spec, long * 3) == 'too_long'
    # A style rewrite may not drop or change the item title.
    assert _faithful_rewrite('风险判断：属低风险，建议6个月后复查。', '风险判断：属于低风险，建议6个月后复查。')
    assert not _faithful_rewrite('风险判断：属低风险，建议6个月后复查。', '属于低风险，建议6个月后复查超声检查。')
    # Follow-up text, key result and first measure of one priority are written in one batch.
    plan = [
        _target(f'{field}{index}', ['comprehensive_conclusion', collection, index, field])
        for index in range(2)
        for collection, field in (
            ('next_steps', 'description'),
            ('priority_directions', 'basis'),
            ('priority_directions', 'followup_action'),
        )
    ]
    batches = _batches(plan)
    assert len(batches) == 1 and [target['path'][2] for target in batches[0]] == [0, 0, 0, 1, 1, 1]
    measure = _spec(plan[2])
    assert measure['form'] == 'label' and '时限；首要事项；科室；后续复查' in measure['guide']
    # Short cells that state a result or a measure go through the medical review like prose.
    assert measure['reviewed'] and _spec(plan[1])['reviewed']
    good = [{'text': '2周内；复查粪便常规和隐血；消化内科；仍阳性时尽早行结肠镜', 'factIds': [], 'cite': []}]
    loose = [{'text': '先到肝胆外科随诊评估，近期完成；肝胆外科；此后每6个月复查胆囊超声', 'factIds': [], 'cite': []}]
    assert _check(measure, good, strict=True) is None
    assert _check(measure, loose, strict=True) == 'bad_format' and _check(measure, loose) is None


def test_ready_related_summaries_share_a_bounded_batch_without_merging_details():
    summaries = [
        _target(f'summary-{field}', ['microbiome_assessment', field, 'text'], owner={'aggregate': True})
        for field in ('structure_interpretation', 'key_microbial_signals', 'scfa_interpretation')
    ]
    assert _batches(summaries) == [summaries]
    # A ready call is not permission to combine arbitrary detailed tables.
    details = [
        _target('structure', ['microbiome_assessment', 'overall_structure', 0, 'explanation']),
        _target('metric', ['microbiome_assessment', 'health_metrics', 0, 'explanation']),
    ]
    assert _batches(details) == [[details[0]], [details[1]]]
    # Large aggregate output stays bounded instead of becoming a whole-report call.
    overview = [
        _target('overview', ['health_overview', 'overall_summary', 'text'], owner={'aggregate': True}),
        _target('score', ['health_overview', 'overall_score', 'explanation'], owner={'aggregate': True}),
        *[
            _target(f'domain-{name}', ['health_overview', 'summaries', name, 'text'], owner={'aggregate': True})
            for name in ('medical', 'microbiome', 'nutrition')
        ],
    ]
    assert len(_batches(overview)) == 2
    assert [target for batch in _batches(overview) for target in batch] == overview


def test_aggregate_category_rows_are_not_split_at_the_cell_limit():
    targets = [
        _target(f'{index}-{field}', ['disease_risk_assessment', 'categories', index, field], owner={'aggregate': True})
        for index in range(8)
        for field in ('clinical_correlation', 'attention_basis', 'summary')
    ]
    batches = _batches(targets)
    assert list(map(len, batches)) == [9, 9, 6]
    assert [target for batch in batches for target in batch] == targets
    for index in range(8):
        assert sum(any(target['path'][2] == index for target in batch) for batch in batches) == 1


def _summary_evidence_targets():
    return [
        _target('alpha', ['microbiome_assessment', 'structure_interpretation', 'text'], owner={'aggregate': True}),
        _target('beta', ['microbiome_assessment', 'key_microbial_signals', 'text'], owner={'aggregate': True}),
    ]


def test_summary_merge_preserves_every_original_publication_under_the_existing_library_budget():
    targets = _summary_evidence_targets()
    summaries = [
        {
            'number': index,
            'title': f'Publication {index}',
            'year': 2024,
            'category': 'guideline',
            'sourceText': 'alpha ' + 'a' * 1990,
            'topicLinks': [
                {
                    'topicId': f't-{index}',
                    'targetIds': ['alpha'],
                    'score': 10,
                    'objectName': 'alpha',
                    'objectTerms': ['alpha'],
                }
            ],
        }
        for index in range(1, 22)
    ]
    summaries.append(
        {
            'number': 22,
            'title': 'Independent beta evidence',
            'year': 2024,
            'category': 'guideline',
            'sourceText': 'beta ' + 'b' * 1991,
            'topicLinks': [
                {
                    'topicId': 't-beta',
                    'targetIds': ['beta'],
                    'score': 1,
                    'objectName': 'beta',
                    'objectTerms': ['beta'],
                }
            ],
        }
    )
    evidence = SimpleNamespace(plan=[], references=[], summaries=summaries)
    alpha = _Library(evidence, targets[:1], 'microbiomeAssessment')
    beta = _Library(evidence, targets[1:], 'microbiomeAssessment')
    crowded = _Library(evidence, targets, 'microbiomeAssessment')
    assert len(alpha.entries) == 21 and [entry['ref'] for entry in beta.entries] == ['E22']
    assert 'E22' not in {entry['ref'] for entry in crowded.entries}
    # Enough evidence for alpha is not a substitute for beta's independent evidence.
    batches = _batches(targets, evidence=evidence, chapter_code='microbiomeAssessment')
    assert batches == [[targets[0]], [targets[1]]]


def test_summary_merge_keeps_original_passages_when_combined_terms_change_selection():
    targets = _summary_evidence_targets()
    alpha_text, beta_text = 'alpha ' + 'a' * 1290, 'beta ' + 'b' * 1291
    evidence = SimpleNamespace(
        plan=[],
        references=[],
        summaries=[
            {
                'number': 1,
                'title': 'Two distinct findings',
                'year': 2024,
                'category': 'guideline',
                'sourceText': alpha_text + '\n' + beta_text,
                'topicLinks': [
                    {'topicId': name, 'targetIds': [name], 'score': 2, 'objectName': name, 'objectTerms': [name]}
                    for name in ('alpha', 'beta')
                ],
            }
        ],
    )
    alpha = _Library(evidence, targets[:1], 'microbiomeAssessment')
    beta = _Library(evidence, targets[1:], 'microbiomeAssessment')
    combined = _Library(evidence, targets, 'microbiomeAssessment')
    assert alpha.entries[0]['passages'][0]['text'] == alpha_text
    assert beta.entries[0]['passages'][0]['text'] == beta_text
    assert [entry['ref'] for entry in combined.entries] == ['E1']
    assert beta_text not in [part['text'] for part in combined.entries[0]['passages']]
    # Keeping the publication alone is insufficient when its beta passage was displaced.
    assert _batches(targets, evidence=evidence, chapter_code='microbiomeAssessment') == [targets[:1], targets[1:]]


def test_summary_merge_keeps_all_original_cell_records_without_raising_the_shared_quota():
    targets = _summary_evidence_targets()
    blueprint = _blueprint(targets)
    facts = [
        {'factId': f'micro-{index}', 'kind': 'microbiome', 'name': f'功能{index}', 'value': index, 'status': '偏低'}
        for index in range(300)
    ]
    blueprint.facts.extend(facts)
    targets[0]['owner']['primaryFactIds'] = [row['factId'] for row in facts[:150]]
    targets[1]['owner']['primaryFactIds'] = [row['factId'] for row in facts[150:]]
    dossier = _Dossier(blueprint, None)
    individual = [dossier.view([target['owner']['primaryFactIds']]) for target in targets]
    combined = dossier.view([target['owner']['primaryFactIds'] for target in targets])
    assert [len(view['cellRecords']) for view in individual] == [150, 150]
    assert len(combined['cellRecords']) == 240
    assert all(view['clinicalRecords'] == combined['clinicalRecords'] for view in individual)
    evidence = SimpleNamespace(plan=[], references=[], summaries=[])
    assert _batches(targets, evidence=evidence, chapter_code='microbiomeAssessment', dossier=dossier) == [
        targets[:1],
        targets[1:],
    ]
    # At the original quota, a genuinely lossless merge remains permitted.
    targets[0]['owner']['primaryFactIds'] = [row['factId'] for row in facts[:120]]
    targets[1]['owner']['primaryFactIds'] = [row['factId'] for row in facts[150:270]]
    assert _batches(targets, evidence=evidence, chapter_code='microbiomeAssessment', dossier=dossier) == [targets]


def test_merged_summaries_keep_individual_review_verdicts_and_citations():
    targets = [
        _target(f'summary-{field}', ['microbiome_assessment', field, 'text'], owner={'aggregate': True})
        for field in ('structure_interpretation', 'key_microbial_signals', 'scfa_interpretation')
    ]
    blueprint = _blueprint(targets)
    blueprint.chapters[0].update(code='microbiomeAssessment', title='菌群评估', number=3)
    evidence = _evidence(targets[0]['id'])
    evidence.summaries[0]['topicLinks'][0]['targetIds'] = [target['id'] for target in targets]

    class Model:
        def __init__(self):
            self.calls = []

        async def generate(self, payload):
            self.calls.append(payload)
            if payload['task'] == 'write_report_chapter':
                return {
                    'cells': [
                        {
                            'id': cell['id'],
                            'paragraphs': [
                                {
                                    'text': '粪便隐血弱阳性需要在消化内科明确出血来源，结合排便表现安排结肠镜检查。'
                                    * 5,
                                    'factIds': ['F001'],
                                    'cite': ['E1.1'],
                                }
                            ],
                        }
                        for cell in payload['cells']
                    ]
                }
            assert payload['task'] == 'review_report_chapter'
            return {'reviews': [{'id': cell['id'], 'verdict': 'pass', 'issues': []} for cell in payload['cells']]}

    model, audit = Model(), {}
    blocks = asyncio.run(
        author_report_chapters(model, blueprint=blueprint, evidence=evidence, subject=None, deadline=None, audit=audit)
    )
    assert [call['task'] for call in model.calls] == ['write_report_chapter', 'review_report_chapter']
    assert [cell['id'] for cell in model.calls[1]['cells']] == ['C01', 'C02', 'C03']
    assert {block['targetId'] for block in blocks['microbiomeAssessment']} == {target['id'] for target in targets}
    for block in blocks['microbiomeAssessment']:
        assert block['sentences'][0]['factIds'] == ['fact-fobt']
        assert block['sentences'][0]['evidence'][0]['number'] == 1
    assert audit['writerUnresolved'] == []


def test_writer_records_outer_gate_wait_and_emits_an_isolated_reviewed_checkpoint(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('modules.report.evaluation_report_writer.monotonic', lambda: now[0])
    target = _target('report:t1', ['medical_assessment', 'trigger_conclusion', 'text'])
    model = FakeModel([lambda payload: {'reviews': [{'id': 'C01', 'verdict': 'pass', 'issues': []}]}])
    audit, checkpoints = {}, []

    def checkpoint(chapter, blocks, unresolved):
        assert model.calls[-1]['task'] == 'review_report_chapter'
        checkpoints.append((chapter, blocks[0]['targetId'], unresolved))
        blocks.clear()  # the caller cannot mutate the writer's checkpoint

    async def run():
        gate = asyncio.Semaphore(0)
        task = asyncio.create_task(
            author_report_chapters(
                model,
                blueprint=_blueprint([target]),
                evidence=_evidence(target['id']),
                subject=None,
                deadline=None,
                audit=audit,
                model_gate=gate,
                on_checkpoint=checkpoint,
            )
        )
        for _ in range(3):
            await asyncio.sleep(0)
        assert model.calls == []
        now[0] = 2.0
        gate.release()
        return await task

    blocks = asyncio.run(run())
    assert blocks['medicalAssessment'][0]['targetId'] == target['id']
    assert checkpoints == [('medicalAssessment', target['id'], [])]
    calls = [row for row in audit['writerTimings'] if row['stage'] == 'model_call']
    assert calls[0]['gateWaitMs'] == 2000.0
    assert all(row['serviceMs'] >= 0 and row['status'] == 'success' for row in calls)
    batch = next(row for row in audit['writerTimings'] if row['stage'] == 'batch')
    assert batch['readyAtMonotonic'] <= batch['startedAtMonotonic'] <= batch['finishedAtMonotonic']
    assert batch['status'] == 'completed' and batch['durationMs'] == 2000.0


def test_writer_closes_timing_when_cancelled_while_waiting_for_shared_gate():
    audit = {}

    async def run():
        model = FakeModel([])
        task = asyncio.create_task(
            author_report_chapters(
                model,
                blueprint=_blueprint([_target('t', ['medical_assessment', 'trigger_conclusion', 'text'])]),
                evidence=_evidence('t'),
                subject=None,
                deadline=None,
                audit=audit,
                model_gate=asyncio.Semaphore(0),
            )
        )
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('writer did not preserve cancellation')
        assert model.calls == []

    asyncio.run(run())
    assert audit['writerTimings']
    assert all(row['status'] == 'cancelled' for row in audit['writerTimings'])
    assert all(row['finishedAtMonotonic'] is not None for row in audit['writerTimings'])
    assert audit.get('reviewedBlocks', {}) == {}


def test_a_failed_batch_checkpoint_cancels_and_joins_other_batches_in_the_same_chapter():
    audit, checkpoints = {}, []
    targets = [
        _target('first', ['microbiome_assessment', 'overall_structure', 0, 'explanation'], owner={'name': 'first'}),
        _target('blocked', ['microbiome_assessment', 'health_metrics', 0, 'explanation'], owner={'name': 'blocked'}),
    ]

    async def run():
        blocked_started, blocked_cancelled = asyncio.Event(), asyncio.Event()

        class Model:
            async def generate(self, payload):
                if payload['task'] == 'write_report_chapter':
                    if payload['cells'][0]['item'] == 'blocked':
                        blocked_started.set()
                        try:
                            await asyncio.Future()
                        finally:
                            blocked_cancelled.set()
                    await blocked_started.wait()
                    return {
                        'cells': [
                            {
                                'id': cell['id'],
                                'paragraphs': [
                                    {
                                        'text': '粪便隐血弱阳性提示出血，需要结合排便情况查因。' * 5,
                                        'factIds': ['F001'],
                                        'cite': [],
                                    }
                                ],
                            }
                            for cell in payload['cells']
                        ]
                    }
                assert payload['task'] == 'review_report_chapter'
                return {'reviews': [{'id': cell['id'], 'verdict': 'pass', 'issues': []} for cell in payload['cells']]}

        def fail_checkpoint(chapter, blocks, unresolved):
            checkpoints.append([block['targetId'] for block in blocks])
            raise RuntimeError('checkpoint failed')

        try:
            await author_report_chapters(
                Model(),
                blueprint=_blueprint(targets),
                evidence=_evidence('first'),
                subject=None,
                deadline=None,
                audit=audit,
                on_checkpoint=fail_checkpoint,
            )
        except RuntimeError as error:
            assert str(error) == 'checkpoint failed'
        else:
            raise AssertionError('writer did not propagate checkpoint failure')
        assert blocked_started.is_set() and blocked_cancelled.is_set()
        assert checkpoints == [['first']]

    asyncio.run(run())
    batches = [row for row in audit['writerTimings'] if row['stage'] == 'batch']
    assert {row['status'] for row in batches} == {'failed', 'cancelled'}
    assert all(row['finishedAtMonotonic'] is not None for row in batches)


def test_record_presence_index_retains_other_record_names_without_implying_results():
    blueprint = _blueprint([])
    blueprint.facts.extend(
        [
            {'factId': 'ecoli', 'kind': 'microbe', 'name': '大肠埃希氏菌', 'value': '0', 'status': '未检出'},
            {'factId': 'ecoli-panel', 'kind': 'microbe', 'name': '大肠埃希氏菌', 'value': '0', 'status': '未检出'},
            {'factId': 'bifido', 'kind': 'microbe', 'name': '双歧杆菌属', 'value': '3.3580', 'status': '正常'},
        ]
    )
    dossier = _Dossier(blueprint, None)
    view = dossier.view([['fact-fobt']])
    assert view['cellRecords'] == []  # the chosen clinical record is already in clinicalRecords
    assert {row['name'] for row in view['recordPresenceIndex']} >= {'大肠埃希氏菌', '双歧杆菌属'}
    assert sum(row['name'] == '大肠埃希氏菌' for row in view['recordPresenceIndex']) == 1
    assert all(set(row) == {'id', 'name', 'kind'} for row in view['recordPresenceIndex'])
    view['recordPresenceIndex'].clear()
    assert dossier.view()['recordPresenceIndex']  # indexes returned to a caller are isolated


def test_medical_review_receives_its_full_cell_duty_and_conservative_comparison_scope():
    target = _target(
        'report:t1',
        ['medical_assessment', 'trigger_conclusion', 'text'],
        owner={
            'aggregate': True,
            'actualPersonalProblems': ['隐血查因'],
            'summaryMode': 'personal_management',
            'primaryFactIds': ['fact-fobt', 'fact-tg', 'not-in-records'],
        },
    )
    model = FakeModel([lambda payload: {'reviews': [{'id': 'C01', 'verdict': 'pass', 'issues': []}]}])
    asyncio.run(
        author_report_chapters(
            model,
            blueprint=_blueprint([target]),
            evidence=_evidence(target['id']),
            subject=None,
            deadline=None,
        )
    )
    draft = model.calls[0]['cells'][0]
    reviewed = next(call for call in model.calls if call['task'] == 'review_report_chapter')['cells'][0]
    for field in ('guide', 'outline', 'scope', 'comparisonScope'):
        assert reviewed[field] == draft[field]
    assert reviewed['scope']['actualPersonalProblems'] == ['隐血查因']
    assert reviewed['comparisonScope']['complete'] is False
    assert len(reviewed['comparisonScope']['groups']) == 2  # different reference/units remain separate
    index = model.calls[0]['patient']['recordPresenceIndex']
    assert index and all('value' not in row and 'status' not in row for row in index)


def test_followup_format_guide_has_no_patient_specific_clinical_sequence():
    spec = _spec(_target('action', ['comprehensive_conclusion', 'priority_directions', 0, 'followup_action']))
    assert '时限；首要事项；科室；后续复查' in spec['guide']
    assert spec['segments'] == 4 and spec['max'] == 64
    assert not any(term in spec['guide'] for term in ('隐血', '粪便', '结肠镜', '胆囊', '仍阳性'))


def test_substantive_prior_corrections_reach_writer_and_reviewer_without_local_citation_handles():
    target = _target('report:t1', ['medical_assessment', 'trigger_conclusion', 'text'])
    prior = {
        'stage': 'review',
        'chapter': 'medicalAssessment',
        'batch': 1,
        'verdicts': {'C01': 'revised'},
        'targets': {
            'C01': {
                'targetId': 'old',
                'item': '粪便隐血',
                'primaryFactIds': ['fact-fobt'],
                'reviewedText': '查因安排见F001，按E1.2的适用条件判断。',
            }
        },
        'issues': {
            'C01': [
                {'type': 'fact_error', 'detail': 'F001是筛查阳性，不能直接诊断或判断F2分期。'},
                {'type': 'citation_mismatch', 'detail': 'E1.2的适用前提没有满足。'},
                {'type': 'off_task', 'detail': '一处轻微文风问题。'},
            ]
        },
    }
    model = FakeModel([lambda payload: {'reviews': [{'id': 'C01', 'verdict': 'pass', 'issues': []}]}])
    audit = {'writer': [prior]}
    asyncio.run(
        author_report_chapters(
            model,
            blueprint=_blueprint([target]),
            evidence=_evidence(target['id']),
            subject=None,
            deadline=None,
            audit=audit,
            prior_review_findings=[prior],
        )
    )
    for call in model.calls:
        if call['task'] not in {'write_report_chapter', 'review_report_chapter'}:
            continue
        corrections = call['priorReviewFindings']
        assert len(corrections) == 2  # duplicated prior/node notes are merged; style-only issues are omitted
        assert {row['type'] for row in corrections} == {'fact_error', 'citation_mismatch'}
        assert all(row['targetId'] == 'old' and row['factIds'] == ['fact-fobt'] for row in corrections)
        assert all('E1.2' not in row['detail'] + row['reviewedText'] for row in corrections)
        assert all('F001' not in row['detail'] + row['reviewedText'] for row in corrections)
        assert any('F2分期' in row['detail'] for row in corrections)
        assert call['patient']['clinicalRecords'][0]['id'] == 'F001'  # current batch handles are still valid
    event = [row for row in audit['writer'] if row['stage'] == 'review'][-1]
    assert event['targets']['C01']['targetId'] == target['id']
    assert event['targets']['C01']['primaryFactIds'] == ['fact-fobt']
    assert 'F001' in prior['issues']['C01'][0]['detail']  # source diagnostics were not edited
