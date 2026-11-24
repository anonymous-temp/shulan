"""Which web pages may serve as report evidence, and how they are classified.

Web search widens recall beyond the curated guideline/literature library, so
admission is deliberately an allow-list: health authorities and professional
societies, peer-reviewed publishers and indexes, and a few professional
references. Patient-education pages, news, social media, citation aggregators
and mirrors are never report evidence, however highly a search engine ranks
them. The list is a business rule; it carries no patient data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit

# Health authorities, guideline bodies and professional societies.
_GUIDELINE_HOSTS = (
    'nhc.gov.cn',
    'chinacdc.cn',
    'who.int',
    'nice.org.uk',
    'sign.ac.uk',
    'uspreventiveservicestaskforce.org',
    'cdc.gov',
    'ahrq.gov',
    'guide.medlive.cn',
    'ccdas.pmphai.com',
    'cma.org.cn',
    'csco.org.cn',
    'easl.eu',
    'aasld.org',
    'gastro.org',
    'gi.org',
    'worldgastroenterology.org',
    'escardio.org',
    'acc.org',
    'heart.org',
    'diabetes.org',
    'idf.org',
    'endocrine.org',
    'thyroid.org',
    'idsociety.org',
    'escmid.org',
    'fascrs.org',
    'asge.org',
    'aasm.org',
    'isappscience.org',
    'wcrf.org',
)
# Peer-reviewed publishers, society journals and bibliographic indexes.
_JOURNAL_HOSTS = (
    'ncbi.nlm.nih.gov',
    'europepmc.org',
    'nature.com',
    'springer.com',
    'springeropen.com',
    'biomedcentral.com',
    'sciencedirect.com',
    'cell.com',
    'thelancet.com',
    'bmj.com',
    'jamanetwork.com',
    'nejm.org',
    'wiley.com',
    'frontiersin.org',
    'mdpi.com',
    'plos.org',
    'asm.org',
    'oup.com',
    'tandfonline.com',
    'karger.com',
    'lww.com',
    'sagepub.com',
    'annualreviews.org',
    'pnas.org',
    'science.org',
    'elifesciences.org',
    'ahajournals.org',
    'diabetesjournals.org',
    'jci.org',
    'aacrjournals.org',
    'wjgnet.com',
    'mednexus.org',
    'yiigle.com',
    'cambridge.org',
    'gastrojournal.org',
    'cghjournal.org',
    'microbiologyresearch.org',
    'portlandpress.com',
    'physiology.org',
    'acpjournals.org',
    'liebertpub.com',
)
# Professional (clinician-facing) references.
_REFERENCE_HOSTS = ('uptodate.com', 'msdmanuals.cn', 'msdmanuals.com', 'merckmanuals.com')

_EXCLUDED_PATH = re.compile(
    r'/topics/|_reference\.pdf|/figures?/|/search|backlinks|/citedby|/metrics|/login|/tag/|/keyword|'
    r'openpeerreview|/news-room/|questions-and-answers|fact-sheets|/news/|/blog/|proof=|subaction=|'
    r'/suppl_?\d*/a\d+',  # meeting abstracts printed in journal supplements
    re.I,
)
_EXCLUDED_HOSTS = ('static-content.springer.com', 'embase.com', 'link-proxy.springer.com')
# Not evidence regardless of the publisher: single cases, news and lay explainers.
_EXCLUDED_TITLE = re.compile(
    r'case report|case series|病例报告|个案报道|第\s*\d+\s*例|[一二三四五六七八九十\d]+例(?:报告|分析)|'
    r'看不懂|科普|一图读懂|你知道吗|[?？]\s*$|press release|新闻稿|erratum|correction to|corrigendum|retraction|'
    r'\bABS-\d+|(?:meeting|conference|congress) abstracts?|会议摘要',
    re.I,
)
_GUIDELINE_TITLE = re.compile(
    r'guideline|consensus|recommendation|position (?:statement|paper)|practice (?:guidance|update|parameter)|'
    r'standards of (?:medical )?care|指南|共识|规范|诊疗方案|筛查与早诊早治|专家建议|推荐意见|推荐流程',
    re.I,
)
# A title that studies a guideline (its impact, adherence or comparison) is a
# study about the guideline, not the guideline itself.
_ABOUT_GUIDELINE = re.compile(
    r'^\W*(?:impact|comparison|adherence|implementation|evaluation|validation|performance|effects?|associations?|'
    r'improving|prevalence|size and|use of|applicability|concordance|agreement)\b|'
    r'\b(?:against|versus|vs\.?|adherence to|compliance with|publish(?:es|ed)?|launch(?:es|ed)?|release[sd]?)\s+'
    r'(?:the\s+)?(?:\w+\s+){0,6}guidelines?\b|experience\b.{0,80}\bguidelines?\b',
    re.I,
)
_STUDY_TYPES = (
    ('Meta-Analysis', re.compile(r'meta[- ]?analy|荟萃分析|meta分析', re.I)),
    ('Systematic Review', re.compile(r'systematic review|系统(?:综述|评价)', re.I)),
    ('Randomized Controlled Trial', re.compile(r'randomi[sz]ed|随机对照', re.I)),
    ('Cohort Study', re.compile(r'\bcohort\b|prospective study|队列', re.I)),
    (
        'Animal or In Vitro Study',
        re.compile(
            r'\b(?:mice|mouse|murine|rats?|rodents?|hamsters?|rabbits?|zebrafish|drosophila|nematodes?|in vitro|'
            r'cell lines?)\b|小鼠|大鼠|体外|动物实验',
            re.I,
        ),
    ),
    ('Review', re.compile(r'\breview\b|overview|综述|研究进展', re.I)),
)


@dataclass(frozen=True)
class WebSourceClass:
    tier: str  # guideline_body | peer_reviewed | professional_reference
    category: str  # guideline | literature (existing evidence categories)
    study_types: tuple[str, ...]
    canonical_url: str
    pmcid: str | None
    doi: str | None


def _host_in(host: str, names: tuple[str, ...]) -> bool:
    return any(host == name or host.endswith('.' + name) for name in names)


def _doi_from_url(parts) -> str | None:
    host, path = (parts.hostname or '').lower(), parts.path
    match = re.search(r'(10\.\d{4,9}/[^\s?#]+)', path)
    if match:
        return match[1].rstrip('/.').removesuffix('/full').removesuffix('/abstract').removesuffix('.pdf').casefold()
    if host.endswith('nature.com'):
        article = re.fullmatch(r'/articles/([a-z0-9-]+)/?', path, re.I)
        if article:
            return '10.1038/' + article[1].casefold()
    return None


def publication_url(url: str | None, doi: str | None) -> str | None:
    """Publish a stable reference without search terms or temporary access tokens."""
    if doi and re.fullmatch(r'10\.\d{4,9}/[^\s<>?#]+', doi.strip(), re.I):
        return 'https://doi.org/' + doi.strip()
    if not url:
        return url
    try:
        parts = urlsplit(url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username is not None:
            return None
    except ValueError:
        return None
    transient = {'search', 'searchterm', 'tracking', 'token', 'access_token', 'signature', 'expires',
                 'key-pair-id', 'policy', 'casa_token', 'guestaccesskey', 'gclid', 'fbclid', '_gl', '_ga'}
    parameters = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True)
                  if name.lower() not in transient and not name.lower().startswith(('utm_', 'x-amz-', 'x-goog-'))]
    if parts.path.rstrip('/').lower().endswith('/action/showpdf') and not any(
        name.lower() in {'pii', 'doi'} and value for name, value in parameters
    ):
        return None
    return parts._replace(query=urlencode(parameters)).geturl()


def evidence_document_title(title: str | None, source_text: str | None) -> str | None:
    """Reject search excerpts presented as publication metadata."""
    title = clean_page_title(title or '')
    if not title:
        return None
    literal = re.sub(r'\s+', ' ', title).casefold().rstrip('….')
    if not source_text or len(literal) < 30:
        return title
    dangling = re.search(r'\b(?:in|of|and|or|to|with|for|by|as|from|into|on|at)$|[，；、（]$', literal)
    if dangling and literal in re.sub(r'\s+', ' ', source_text.casefold()):
        return None
    for line in source_text.splitlines():
        content = re.sub(r'\s+', ' ', line).strip()
        if content.casefold() in {literal, literal + '.'}:
            continue
        continuation = re.match(r'(?:\s+[a-z]|[,，;；]|[A-Za-z])', content[len(literal):])
        if content.casefold().startswith(literal) and continuation:
            return None
    return title


def classify_web_source(url: str, title: str = '') -> WebSourceClass | None:
    """Return the evidence class of an admissible page; ``None`` rejects it."""
    try:
        parts = urlsplit(str(url or ''))
    except ValueError:
        return None
    host = (parts.hostname or '').lower()
    if parts.scheme not in {'http', 'https'} or not host:
        return None
    if _EXCLUDED_PATH.search(parts.path) or _EXCLUDED_PATH.search(parts.query or ''):
        return None
    if _host_in(host, _EXCLUDED_HOSTS) or _EXCLUDED_TITLE.search(str(title or '')):
        return None
    pmc = re.search(r'\bPMC(\d{4,9})\b', parts.path + '?' + (parts.query or ''), re.I)
    pmcid = 'PMC' + pmc[1] if pmc else None
    if host.endswith('ncbi.nlm.nih.gov') and not (
        pmcid or re.search(r'/books/NBK\d+|^/\d+/?$', parts.path) or host.startswith('pubmed.')
    ):
        # Other NCBI/NLM properties include patient pages and tool front ends.
        return None
    if _host_in(host, _GUIDELINE_HOSTS):
        tier = 'guideline_body'
    elif _host_in(host, _JOURNAL_HOSTS):
        tier = 'peer_reviewed'
    elif _host_in(host, _REFERENCE_HOSTS):
        if 'msdmanuals' in host and '/professional' not in parts.path and '/home/' in parts.path:
            return None  # consumer edition
        tier = 'professional_reference'
    else:
        return None
    text = str(title or '')
    guideline = (bool(_GUIDELINE_TITLE.search(text)) and not _ABOUT_GUIDELINE.search(text)) or (
        tier == 'guideline_body'
        and bool(re.search(r'/guidance/|/recommendation|/guidelines?/|/our-guidelines/', parts.path))
    )
    if tier == 'guideline_body' and not guideline:
        # An authority's site also hosts news, Q&A and lay explainers. Only its
        # guidelines, consensus statements and standards are report evidence.
        return None
    study_types = [name for name, pattern in _STUDY_TYPES if pattern.search(text)]
    if guideline and tier != 'professional_reference':
        study_types.insert(0, 'Practice Guideline')
    if tier == 'professional_reference':
        study_types.insert(0, 'Professional Reference')
    if pmcid:
        canonical = f'https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/'
    elif '__cf_chl' in (parts.query or ''):  # a bot-challenge token is not part of the address
        canonical = parts._replace(query='', fragment='').geturl()
    else:
        canonical = parts._replace(fragment='').geturl()
    return WebSourceClass(
        tier=tier,
        category='guideline' if guideline and tier != 'professional_reference' else 'literature',
        study_types=tuple(dict.fromkeys(study_types)),
        canonical_url=canonical,
        pmcid=pmcid,
        doi=_doi_from_url(parts),
    )


# Evidence grade shown in the bibliography. It ranks the *type* of source and
# is not a GRADE certainty assessment of any individual recommendation.
_GRADE_ORDER = (
    ('A', '指南/共识', re.compile(r'guideline|consensus|practice guideline|指南|共识', re.I)),
    ('A', '系统综述/Meta分析', re.compile(r'meta[- ]?analysis|systematic review', re.I)),
    ('B', '随机对照试验', re.compile(r'randomi[sz]ed controlled trial|clinical trial', re.I)),
    ('B', '队列/观察性研究', re.compile(r'cohort|observational|case-control|cross-sectional|comparative study', re.I)),
    ('C', '综述', re.compile(r'\breview\b', re.I)),
    ('C', '专业参考', re.compile(r'professional reference', re.I)),
)


_ANIMAL_TITLE = re.compile(
    r'\b(?:mice|mouse|murine|rats?|rodents?|hamsters?|rabbits?|piglets?|zebrafish|drosophila|nematodes?|in vitro|'
    r'animal (?:studies|models?|experiments?))\b|小鼠|大鼠|动物实验|动物模型',
    re.I,
)


def evidence_grade(category: str | None, study_types, title: str = '') -> tuple[str, str]:
    """Map existing category/publication types to a displayed source grade.

    A guideline/consensus/systematic review; B trial or cohort; C narrative
    review, other human research or professional reference; D animal or
    in-vitro research. Library channels label some reviews as guidelines, so
    when the title is known a guideline label also needs a guideline-like
    title; a title naming animal or in-vitro work always grades D.
    """
    types = ' | '.join(str(value) for value in (study_types or ()))
    title = str(title or '')
    if (
        title
        and _ANIMAL_TITLE.search(title)
        and not re.search(r'\b(?:humans?|patients?|adults?)\b|患者|人群', title, re.I)
    ):
        return 'D', '动物或体外研究'
    if category == 'guideline' and (not title or _GUIDELINE_TITLE.search(title) and not _ABOUT_GUIDELINE.search(title)):
        return 'A', '指南/共识'
    if re.search(r'animal or in vitro|\banimals?\b', types, re.I) and not re.search(r'\bhumans?\b', types, re.I):
        return 'D', '动物或体外研究'
    for grade, label, pattern in _GRADE_ORDER[1:]:
        if title and pattern.search(title):
            return grade, label
    for grade, label, pattern in _GRADE_ORDER:
        if pattern.search(types):
            return grade, label
    return 'C', '研究论文'


_SOURCE_NAMES = {
    'rs.yiigle.com': '中华医学期刊全文数据库',
    'cmab.yiigle.com': '中华医学知识库',
    'ccdas.pmphai.com': '人卫临床助手',
    'guide.medlive.cn': '医脉通指南库',
    'www.nhc.gov.cn': '国家卫生健康委员会',
    'pmc.ncbi.nlm.nih.gov': 'PubMed Central',
    'pubmed.ncbi.nlm.nih.gov': 'PubMed',
    'www.uptodate.com': 'UpToDate',
    'www.msdmanuals.cn': '默沙东诊疗手册（专业版）',
    'europepmc.org': 'Europe PMC',
    'www.ebi.ac.uk': 'Europe PMC',
    'www.sciencedirect.com': 'ScienceDirect',
}
_SITE_NAMES = {'europepmc.org': 'Europe PMC', 'sciencedirect.com': 'ScienceDirect', 'ncbi': 'PubMed Central'}


def source_display_name(url: str, site_name: str | None = None) -> str:
    """Publisher/library name for a bibliography row when no journal is known."""
    host = (urlsplit(str(url or '')).hostname or '').lower()
    if host in _SOURCE_NAMES:
        return _SOURCE_NAMES[host]
    name = str(site_name or '').strip()
    name = _SITE_NAMES.get(name.casefold(), name)
    if name and name not in {'百度学术', 'Ncbi', 'europepmc.org'} and '.' not in name.rstrip('.'):
        return name
    return name if name and name not in {'百度学术'} else host


def is_figure_or_table_caption(title: str) -> bool:
    return bool(re.match(
        r'^\s*(?:(?:supplementary\s+)?(?:fig(?:ure)?|table|scheme)\.?\s*S?\d+[A-Za-z]?\s*[.:：)]|'
        r'[图表]\s*\d+\s*[.、：:])', title, re.I,
    ))


def clean_page_title(title: str) -> str | None:
    """Recover a document title from a search-result title; ``None`` if it is a fragment."""
    text = re.sub(r'<[^>]+>', '', str(title or ''))
    text = re.sub(r'\*\*|^#+\s*', '', text).strip()
    text = re.sub(r'^title\s*[:：]\s*', '', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip()
    if '>' in text:  # breadcrumb: the document is the last element
        text = text.rsplit('>', 1)[1].strip()
    parts = [part.strip() for part in re.split(r'\s+[-–—|]\s+', text) if part.strip()]
    if parts:
        minimum = 4 if re.search(r'[\u4e00-\u9fff]', parts[0]) else 15
        text = parts[0] if len(parts[0]) >= minimum else max(parts, key=len)
    text = text.strip(' .·•-')
    if len(text) < 6 or len(text) > 260:
        return None
    if is_figure_or_table_caption(text):
        return None
    if re.match(
        r'^(?:\d{1,2}(?:\.\d+)*\s+(?:本(?:共识|指南|规范|建议)|'
        r'(?:Introduction|Background|Methods|Results|Discussion|Conclusions)\b)|'
        r'第[一二三四五六七八九十百\d]+[章节]\s*)', text, re.I,
    ):
        return None  # a numbered body section is not the publication's identity
    if re.match(r'^[^A-Za-z\u4e00-\u9fff《【(（"“\d]', text) or re.match(r'^[a-z]', text):
        return None  # starts mid-sentence
    if re.search(r',\d{1,3}\s+(?:therefore|however|we)\b|\b(?:we|therefore)\s+(?:investigated|found)\b', text):
        return None
    if re.search(
        r'^(?:recent|previous|several|many) (?:research|studies|reports)\b|\b(?:has|have) (?:been )?shown\b|'
        r',\s*which\b',
        text,
        re.I,
    ):
        return None  # a sentence from the page body, not a title
    return text


__all__ = ['WebSourceClass', 'classify_web_source', 'clean_page_title', 'evidence_grade', 'source_display_name']
