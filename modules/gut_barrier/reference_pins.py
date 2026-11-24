"""Versioned, reviewed bibliography prefix requested on 2026-10-08.

Pinning controls display order only. It does not assert a claim-to-source
relationship, reclassify clinical evidence, or influence risk scoring. No user
filesystem paths or live EviMed search results are required during generation.
"""
from copy import deepcopy

PINNED_REFERENCE_VERSION = "gut-barrier-pinned-references-v1.20261008"
_PUBLIC_FIELDS = ("citation", "url", "year", "publisher", "doi", "location")

_CATALOG = ({'citation': '李兰娟. 感染微生态研究进展——肠道菌群对机体代谢影响[J]. 中国微生态学杂志,2009,21(1):1-3. '
              'DOI:10.13381/j.cnki.cjm.2009.01.018.',
  'url': 'https://www.sinomed.ac.cn/zh/detail.do?ui=2010489079',
  'year': 2009,
  'publisher': '中国微生态学杂志',
  'doi': '10.13381/j.cnki.cjm.2009.01.018',
  'location': '21(1):1-3',
  'provenance': {'source': 'EviMed V2 and user supplied citation',
                 'evimedId': '3_372_6472c8623c1179a8f1fc9b92',
                 'matchedBy': 'exact title, author, journal and year',
                 'apiVerifiedFields': ['title', 'author', 'journal', 'year', 'url'],
                 'userProvidedFields': ['doi', 'volume', 'issue', 'pages'],
                 'verifiedAt': '2026-10-08',
                 'scope': '感染微生态及肠道菌群与代谢的综述背景；不用于验证风险分值或个人疾病概率。'}},
 {'citation': '中华医学会消化病学分会胃肠功能性疾病协作组,中华医学会消化病学分会胃肠动力学组. 2020年中国肠易激综合征专家共识意见[J]. '
              '中华消化杂志,2020,40(12):803-818. DOI:10.3760/cma.j.cn311367-20201116-00660.',
  'url': 'https://doi.org/10.3760/cma.j.cn311367-20201116-00660',
  'year': 2020,
  'publisher': '中华消化杂志',
  'doi': '10.3760/cma.j.cn311367-20201116-00660',
  'location': '40(12):803-818',
  'provenance': {'source': 'user supplied PDF',
                 'archive': '文献.zip',
                 'archiveSha256': 'bda0da180ccf08ebdeb60dc302eb47d90c53c6cade340f23e571adc1bb9308e0',
                 'archiveOrder': 1,
                 'providedFile': '2020年中国肠易激综合征专家共识意见.pdf',
                 'fileSha256': '35d798abb1ef4e1a6c8237fe903e213095f3f9f1b0cbf2ec506e99a79e1f71be',
                 'verifiedAt': '2026-10-08',
                 'metadataEvidence': '首页“引用本文”、题名及摘要；首页已渲染目视核验。',
                 'scope': '用于肠易激综合征的定义、病因与发病机制、诊断及治疗背景；不能直接证明本项目风险分值或阈值。'}},
 {'citation': '中华预防医学会微生态学分会. 中国消化道微生态调节剂临床应用共识（2016版）[J]. 中国微生态学杂志,2016,28(6):621-631. '
              'DOI:10.13381/j.cnki.cjm.201606001.',
  'url': 'https://doi.org/10.13381/j.cnki.cjm.201606001',
  'year': 2016,
  'publisher': '中国微生态学杂志',
  'doi': '10.13381/j.cnki.cjm.201606001',
  'location': '28(6):621-631',
  'provenance': {'source': 'user supplied PDF',
                 'archive': '文献.zip',
                 'archiveSha256': 'bda0da180ccf08ebdeb60dc302eb47d90c53c6cade340f23e571adc1bb9308e0',
                 'archiveOrder': 2,
                 'providedFile': '中国消化道微生态调节剂临床应用共识_2016版_.pdf',
                 'fileSha256': '51f24c5f1224843efb34348bfa592a0a1abed1b7f31ffe273f632978e7cfddf6',
                 'verifiedAt': '2026-10-08',
                 'metadataEvidence': '首页题名、团体作者、刊头、文章编号及DOI，经pypdf和pdfplumber两种提取器交叉确认；首页和末页可视页码为621、631。',
                 'scope': '用于消化道微生态调节剂（益生菌、益生元、合生元）的概念及相关消化道疾病临床应用背景；不构成任意产品或统一剂量的直接推荐。'}},
 {'citation': '中华医学会肠外肠内营养学分会. 成人胃肠功能障碍患者医学营养治疗指南（2025版）[J]. 中华医学杂志,2025,105(1):21-47. '
              'DOI:10.3760/cma.j.cn112137-20240815-01881.',
  'url': 'https://doi.org/10.3760/cma.j.cn112137-20240815-01881',
  'year': 2025,
  'publisher': '中华医学杂志',
  'doi': '10.3760/cma.j.cn112137-20240815-01881',
  'location': '105(1):21-47',
  'provenance': {'source': 'user supplied PDF',
                 'archive': '文献.zip',
                 'archiveSha256': 'bda0da180ccf08ebdeb60dc302eb47d90c53c6cade340f23e571adc1bb9308e0',
                 'archiveOrder': 3,
                 'providedFile': '成人胃肠功能障碍患者医学治疗指南.pdf',
                 'fileSha256': 'dad18ca3507bdd714372159938e4c9d1860325766706a0f9794e35d738619c3b',
                 'verifiedAt': '2026-10-08',
                 'metadataEvidence': '首页题名、摘要及“引用本文”；首页已渲染目视核验。',
                 'scope': '用于成人胃肠功能障碍患者医学营养治疗流程、肠内/肠外营养的应用时机和方式及特定疾病状态下的营养干预背景。'}},
 {'citation': '中华医学会肠外肠内营养学分会神经疾病营养支持学组,中华医学会神经病学分会神经重症协作组,中国医师协会神经内科医师分会神经重症专业委员会,等. '
              '神经系统疾病肠内营养支持中国专家共识（第二版）[J]. 中华临床营养杂志,2019,27(4):193-203. '
              'DOI:10.3760/cma.j.issn.1674-635X.2019.04.001.',
  'url': 'https://doi.org/10.3760/cma.j.issn.1674-635X.2019.04.001',
  'year': 2019,
  'publisher': '中华临床营养杂志',
  'doi': '10.3760/cma.j.issn.1674-635X.2019.04.001',
  'location': '27(4):193-203',
  'provenance': {'source': 'user supplied PDF',
                 'archive': '文献.zip',
                 'archiveSha256': 'bda0da180ccf08ebdeb60dc302eb47d90c53c6cade340f23e571adc1bb9308e0',
                 'archiveOrder': 4,
                 'providedFile': '神经系统疾病肠内营养支持中国专家共识(第二版).pdf',
                 'fileSha256': 'aea6787fa44a20296520f54e2cbc4a7dadeedfb481d9d1bd64de5b8c112a945b',
                 'verifiedAt': '2026-10-08',
                 'metadataEvidence': '首页题名、署名、刊头及DOI；首末页目视核验页码193、203。',
                 'scope': '用于神经系统疾病患者营养支持适应证、肠内营养操作和管理背景；不直接支持肠屏障指标推导普通人群神经疾病风险。'}},
 {'citation': '中国抗癌协会肿瘤与微生态专业委员会. 肠道微生态与造血干细胞移植相关性中国专家共识[J]. 国际肿瘤学杂志,2021,48(3):129-135. '
              'DOI:10.3760/cma.j.cn371439-20210202-00027.',
  'url': 'https://doi.org/10.3760/cma.j.cn371439-20210202-00027',
  'year': 2021,
  'publisher': '国际肿瘤学杂志',
  'doi': '10.3760/cma.j.cn371439-20210202-00027',
  'location': '48(3):129-135',
  'provenance': {'source': 'user supplied PDF',
                 'archive': '文献.zip',
                 'archiveSha256': 'bda0da180ccf08ebdeb60dc302eb47d90c53c6cade340f23e571adc1bb9308e0',
                 'archiveOrder': 5,
                 'providedFile': '肠道微生态与造血干细胞移植相关性中国专家共识.pdf',
                 'fileSha256': 'd0a56bce2129ec5e5e1ef4a44b4592e02c3b585129cd17a0705ce59e1c239f74',
                 'verifiedAt': '2026-10-08',
                 'metadataEvidence': '扫描PDF，首页题名、团体作者、刊头及DOI、末页页码均经渲染目视核验。',
                 'scope': '用于造血干细胞移植人群肠道微生态与感染、移植物抗宿主病及预后的关联背景；该特定人群结论不能直接外推至普通体检人群。'}},
 {'citation': '中华医学会消化病学分会胃肠激素与黏膜屏障学组. 胃肠道黏膜保护临床专家共识（2021年，福州）[J]. 中华消化杂志,2021,41(12):798-811. '
              'DOI:10.3760/cma.j.cn311367-20210930-00524.',
  'url': 'https://doi.org/10.3760/cma.j.cn311367-20210930-00524',
  'year': 2021,
  'publisher': '中华消化杂志',
  'doi': '10.3760/cma.j.cn311367-20210930-00524',
  'location': '41(12):798-811',
  'provenance': {'source': 'user supplied PDF',
                 'archive': '文献.zip',
                 'archiveSha256': 'bda0da180ccf08ebdeb60dc302eb47d90c53c6cade340f23e571adc1bb9308e0',
                 'archiveOrder': 6,
                 'providedFile': '胃肠道黏膜保护临床专家共识.pdf',
                 'fileSha256': 'ae4105a249b19c56f3ec596d9cec46ea07ca1cfd1610ea02ab6515a7439a1b61',
                 'verifiedAt': '2026-10-08',
                 'metadataEvidence': '首页题名、摘要及“引用本文”；首页已渲染目视核验。',
                 'scope': '用于胃肠黏膜屏障生理、黏膜损伤与疾病机制及黏膜保护剂分类和合理应用背景。'}},
 {'citation': '中华医学会肠外肠内营养学分会,中国国际医疗保健促进交流会加速康复外科分会,中国微生态治疗创新联盟,等. 菌群移植标准化方法学的建立与临床应用中国专家共识[J]. '
              '中华胃肠外科杂志,2020,23(增刊):S5-S13. DOI:10.3760/cma.j.cn.441530-20200420-00231.',
  'url': 'https://doi.org/10.3760/cma.j.cn.441530-20200420-00231',
  'year': 2020,
  'publisher': '中华胃肠外科杂志',
  'doi': '10.3760/cma.j.cn.441530-20200420-00231',
  'location': '23(增刊):S5-S13',
  'provenance': {'source': 'user supplied PDF',
                 'archive': '文献.zip',
                 'archiveSha256': 'bda0da180ccf08ebdeb60dc302eb47d90c53c6cade340f23e571adc1bb9308e0',
                 'archiveOrder': 7,
                 'providedFile': '菌群移植标准化方法学的建立与临床应用中国专家共识.pdf',
                 'fileSha256': 'd716cf908214cbeddd9c0602089349c67f833c67ffb035ccd59bc860b54019f8',
                 'verifiedAt': '2026-10-08',
                 'metadataEvidence': '首页题名、团体作者、刊头及DOI；末页“本文引用格式”载明23(增刊):S5-S13。',
                 'scope': '用于菌群移植的标准化方法学、供体与实施管理及临床应用背景；不等同于对普通肠屏障异常者实施菌群移植的建议。'}})


def pinned_references():
    """Li Lanjuan first, followed by the seven PDFs in original ZIP order."""
    return [{key: deepcopy(item[key]) for key in _PUBLIC_FIELDS} for item in _CATALOG]


def pinned_reference_audit():
    return {"version": PINNED_REFERENCE_VERSION, "orderBasis": "Li first, then original ZIP order",
            "mode": "fixed_bibliography_prefix_not_score_validation", "sources": deepcopy(list(_CATALOG))}
