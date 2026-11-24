"""Literal EviMed originals for evaluate; generated summaries are not originals."""
import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from functools import lru_cache


def _plain(record):
    return record.model_dump(mode='json', by_alias=True) if hasattr(record, 'model_dump') else record


def is_trusted_guide_block(record):
    row = _plain(record)
    return bool(isinstance(row, dict) and row.get('provider') == 'evimed_v2'
        and row.get('category') == 'guideline'
        and isinstance(row.get('parentGuideId'), str) and row['parentGuideId'].strip()
        and row.get('sourceId') == row['parentGuideId']
        and isinstance(row.get('sourceEvidenceKey'), str) and row['sourceEvidenceKey'].strip()
        and isinstance(row.get('evidenceKey'), str) and row['evidenceKey'].strip()
        and row['evidenceKey'] != row['sourceEvidenceKey']
        and isinstance(row.get('evidenceText'), str) and row['evidenceText'].strip())


def has_substantive_original_text(text):
    return bool(isinstance(text,str) and text.strip() and not re.fullmatch(
        r'\s*(?:(?:brief|plain language)\s+)?(?:summary|abstract|background|摘要)[\s.:：]*',text,re.I))


def evaluation_original_text(record):
    row = _plain(record)
    text = row.get('evidenceText')
    if (row.get('provider') == 'pmc' and re.fullmatch(r'PMC\d+', str(row.get('sourceId') or ''), re.I)
            and has_substantive_original_text(text)):
        # The existing PMC transport retains the literal complete supplied
        # primary body and its parent abstract. A copied shorter abstract
        # must not hide that separately fetched original.
        return text
    if (row.get('provider') == 'official_guideline'
            and str(row.get('doi') or '').casefold() == '10.5664/jcsm.4758'
            and row.get('parentGuideId') == 'doi:10.5664/jcsm.4758'
            and row.get('sourceId') in {
                'https://www.aasm.org/resources/pdf/adultsleepdurationconsensus.pdf',
                'https://pmc.ncbi.nlm.nih.gov/articles/PMC4442216/'}
            and has_substantive_original_text(text)
            and row.get('sourceEvidenceKey') == 'official-original:sha256:' + hashlib.sha256(text.encode()).hexdigest()):
        # Literal body fetched by the exact-source reader. This only selects
        # the original for fresh citation review; it approves no claim, age
        # applicability, diagnosis or recommendation by serialized metadata.
        return text
    if (row.get('provider') == 'deepseek_web' and has_substantive_original_text(text)
            and isinstance(row.get('sourceId'), str) and row['sourceId'].startswith(('http://', 'https://'))
            and row.get('sourceEvidenceKey') == 'web-original:sha256:' + hashlib.sha256(text.encode()).hexdigest()):
        # Literal page text returned by the search/fetch tool for an
        # allow-listed source. The integrity key proves it was not edited
        # after retrieval; it approves no claim or applicability.
        return text
    abstract = row.get('abstract')
    if (row.get('provider') == 'evimed_v2' and row.get('category') == 'literature'
            and has_substantive_original_text(text)
            and len(text) > len(abstract or '') + 400):
        # The library returns the article body next to its abstract. Using
        # only the abstract left authors with a few sentences per paper.
        if has_substantive_original_text(abstract) and abstract.strip()[:120] not in text:
            return abstract.strip() + '\n\n' + text
        return text
    if has_substantive_original_text(abstract):
        return abstract
    return row['evidenceText'] if is_trusted_guide_block(row) else ''


_REFERENCE_HEADING = re.compile(r'(?:references?|bibliography|literature cited|参考文献|引用文献)\s*[:：]?$', re.I)
_CATALOG_HEADING = re.compile(
    r'(?:supporting information|supplementary (?:information|materials?|files?|data)|'
    r'supplemental (?:information|materials?|files?|data)|appendices|补充材料|补充信息|附件目录)\s*[:：]?$', re.I)
_METADATA_HEADING = re.compile(
    r'(?:author(?:s[’\']?)? contributions?|credit authorship contribution statement|'
    r'conflicts? of interest(?: statement)?|competing interests?|declarations?(?: of interest)?|'
    r'funding(?: information)?|acknowledg(?:e)?ments?|data availability(?: statement)?|'
    r'copyright|article information|author information|作者贡献|利益冲突|资金支持|致谢|数据可用性)\s*[:：]?$', re.I)
_BODY_HEADING = re.compile(
    r'(?:abstract|background|introduction|(?:materials? and )?methods?|results?|discussion|'
    r'conclusions?|recommendations?|limitations?|summary|objectives?|aims?|purpose|'
    r'supplement(?:ary|al) methods?|摘要|背景|引言|目的|方法|结果|讨论|结论|建议|局限性)\s*[:：]?$', re.I)
_CONTEXT_HEADING = re.compile(
    r'^(background|introduction|objectives?|aims?|purpose|(?:materials? and )?methods?|'
    r'supplement(?:ary|al) methods?|背景|引言|目的|方法)\s*(?:[:：]|$)', re.I)
_BIBLIOGRAPHIC_LOCATOR = re.compile(
    r'\b(?:19|20)\d{2}\s*;\s*\d+(?:\s*\([^\n)]{1,24}\))?\s*:\s*'
    r'(?:[A-Z]?\d+(?:\s*[-–—]\s*[A-Z]?\d+)?|[eE]\d+)\b')
_BIBLIOGRAPHIC_AUTHOR = re.compile(
    r'^(?:[A-Z]{1,4}\.?\s+[A-Z][A-Za-z’\'-]+|[A-Z][A-Za-z’\'-]+\s+[A-Z]{1,4}\.?)\s*[,;]')
_CAPTION = re.compile(r'^(?:(?:table|figure|fig\.?|supplementary table|supplementary figure)\s*'
    r'[A-Z]?\d+[A-Z]?\b|(?:表|图)\s*[A-Z]?\d+|(?:table|figure) (?:notes?|footnotes?)\s*[:：]|'
    r'(?:表注|图注|脚注)\s*[:：])', re.I)
_METADATA_LINE = re.compile(r'^(?:keywords?|key words|doi|pmid|pmcid|received|accepted|published online|'
    r'correspond(?:ing author|ence)|copyright|©|all rights reserved|关键词|收稿日期|通信作者)\s*[:： ]', re.I)
_REFERENCE_ENTRY = re.compile(
    r'^\s*(?:[-*]\s*)?(?:\[\d+\]\s*|\d+[.)]\s*)?'
    r'[A-Z][A-Za-z’\'-]+,\s*[A-Z]\.(?:[^\n]{0,180})\(\d{4}[a-z]?\)\.')
_SECONDARY_ATTRIBUTION = re.compile(
    r'\b(?:(?:previous|prior|earlier|other|published|recent)\s+(?:(?:clinical|animal|human|cohort)\s+)?'
    r'(?:studies|study|research|trials?|work|reports?)|according to|'
    r'[A-Z][A-Za-z’\'-]+ et al\.?[^\n.!?]{0,80}\b(?:reported|showed|found|demonstrated|suggested|observed))'
    r'|既往研究|先前研究|其他研究|已有研究|据[^。\n]{1,30}研究', re.I)
_OWN_RECOMMENDATION = re.compile(
    r'\b(?:we|the panel|the (?:guideline|consensus)(?: committee)?|this (?:guideline|consensus)|'
    r'the task force)\s+(?:strongly\s+|conditionally\s+)?(?:recommend|suggest|advise)\w*\b|'
    r'^\s*(?:[-*•]\s*)?(?:recommendation\s+\d+\b|recommend(?:ed)?\s+|建议|推荐)|'
    r'(?:本指南|本共识|专家组|工作组)(?:建议|推荐)', re.I)
_CAPTION_PROVENANCE = re.compile(
    r'^(?:(?:table|figure|fig\.?)\s*[A-Z]?\d+[A-Z]?[.:]?\s*)?'
    r'(?:adapted|reproduced|reprinted|source\s*[:：]|data from|copyright|'
    r'abbreviations?\s*[:：]|(?:table|figure) (?:notes?|footnotes?)\s*[:：]|'
    r'(?:表注|图注|脚注|来源|缩略词)\s*[:：])', re.I)
_RESULT_DATA = re.compile(
    r'\b[qp]\s*(?:=|<|>|≤|≥)\s*\d|\b\d+(?:\.\d+)?\s*(?:%|±)|'
    r'\b(?:odds|hazard|risk) ratio\s*[:=]?\s*\d|\b(?:mean|median|coefficient)\s*[:=]\s*[-+]?\d|'
    r'\|\s*[-+]?\d+(?:\.\d+)?(?:\s*(?:±|%))?', re.I)
_CONTEXT_SECTION_ROLES = {'background_context', 'study_objective', 'study_methods'}
_EVIDENCE_SECTION_ROLES = {
    'primary_body', 'result_data', 'guideline_recommendation', 'unresolved_figure_context',
    *_CONTEXT_SECTION_ROLES,
}


def _context_section_role(heading):
    match = _CONTEXT_HEADING.match(heading)
    if not match:
        return None
    label = match[1].casefold()
    if re.search(r'method|方法', label):
        return 'study_methods'
    if re.search(r'objective|aim|purpose|目的', label):
        return 'study_objective'
    return 'background_context'


def _front_matter_title(text):
    """Recognize an opening title corroborated by the source's own citation.

    An arbitrary H1 may be a real body section. Only the opening H1 whose
    literal title also appears in an author/year/volume/page metadata block
    is treated as article identity rather than as the start of its body.
    """
    first = next((line.strip() for line in text.splitlines() if line.strip()), '')
    match = re.fullmatch(r'#\s+(.+)', first)
    if not match:
        return None
    title = match[1].strip().strip('*_ ')
    if _BODY_HEADING.fullmatch(title) or _CAPTION.match(title):
        return None
    identity = re.sub(r'\W+', '', unicodedata.normalize('NFKC', title)).casefold()
    if not identity:
        return None
    for block in re.split(r'\n\s*\n', text):
        literal = block.strip()
        if (_BIBLIOGRAPHIC_AUTHOR.match(literal) and _BIBLIOGRAPHIC_LOCATOR.search(literal)
                and identity in re.sub(r'\W+', '', unicodedata.normalize('NFKC', literal)).casefold()):
            return title
    return None


@lru_cache(maxsize=96)
def _original_passage_roles(text):
    """Classify exact original spans; never rewrite or truncate the archive.

    Section labels and bibliography syntax are source structure, not evidence
    of the named organism's role in the publication's own study. Attribution
    in real prose remains readable, with secondary/background use explicit.
    """
    result, section, pending, body_heading = [], 'primary_body', [], ''
    article_title = _front_matter_title(text)

    def flush():
        if not pending:
            return
        start, end = pending[0][0], pending[-1][1]
        literal = text[start:end]
        role = section
        if _CAPTION_PROVENANCE.match(literal):
            role = 'table_or_figure_note'
        elif _CAPTION.match(literal) and section == 'primary_body':
            if _RESULT_DATA.search(literal) or re.search(r'\bresults?\b|结果', body_heading, re.I):
                role = 'result_data'
            elif re.search(r'[.!?。！？](?:\s|$)', literal) and len(literal) > 120:
                # A flattened caption may contain actual research prose;
                # keep it readable without asserting this paper owns a result.
                role = 'unresolved_figure_context'
            else:
                role = 'table_or_figure_note'
        elif _METADATA_LINE.match(literal):
            role = 'article_metadata'
        elif _REFERENCE_ENTRY.match(literal):
            role = 'reference_entry'
        elif (not body_heading and _BIBLIOGRAPHIC_AUTHOR.match(literal)
                and _BIBLIOGRAPHIC_LOCATOR.search(literal)):
            # A front-matter journal/year/volume/page citation is not an
            # article proposition, even when it is outside References.
            role = 'article_metadata'
        elif (role == 'primary_body' and re.search(r'\bresults?\b|结果', body_heading, re.I)
                and _RESULT_DATA.search(literal)):
            role = 'result_data'
        if role == 'primary_body':
            role = _context_section_role(literal) or _context_section_role(body_heading) or role
        secondary = role in _EVIDENCE_SECTION_ROLES and bool(_SECONDARY_ATTRIBUTION.search(literal))
        own_recommendation = role in _EVIDENCE_SECTION_ROLES and bool(_OWN_RECOMMENDATION.search(literal))
        if own_recommendation and role not in _CONTEXT_SECTION_ROLES:
            role = 'guideline_recommendation'
        else:
            own_recommendation = False
        citation = ('guideline_recommendation_with_supporting_references' if own_recommendation else
            'secondary_background' if secondary or role == 'unresolved_figure_context' else 'publication_prose')
        result.append((start, end, role, citation))
        pending.clear()

    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        start, end = offset + len(line) - len(line.lstrip()), offset + len(line.rstrip())
        offset += len(line)
        if not stripped:
            flush()
            continue
        heading = re.sub(r'^\s*(?:#{1,6}\s+|\d+(?:\.\d+)*[.)]?\s+)', '', stripped).strip('*_ ')
        if not result and not pending and article_title is not None and heading == article_title:
            result.append((start, end, 'article_title', 'structure_only'))
            continue
        special = ('references' if _REFERENCE_HEADING.fullmatch(heading) else
            'supplement_catalog' if _CATALOG_HEADING.fullmatch(heading) else
            'article_metadata' if _METADATA_HEADING.fullmatch(heading) else None)
        is_heading = bool(re.match(r'^\s*#{1,6}\s+', line)) or bool(_BODY_HEADING.fullmatch(heading))
        if special or is_heading:
            flush()
            if special:
                section = special
            elif _BODY_HEADING.fullmatch(heading) or section == 'primary_body':
                section = 'primary_body'
                if not _CAPTION.match(heading):
                    body_heading = heading
            result.append((start, end, 'section_heading', 'structure_only'))
            continue
        # Keep one caption or metadata record separate from adjacent prose.
        if _CAPTION.match(stripped):
            flush()
            pending.append((start, end))
        elif _METADATA_LINE.match(stripped):
            flush()
            pending.append((start, end))
            flush()
        else:
            pending.append((start, end))
    flush()
    return tuple(result)


def evaluation_original_passages(text):
    """Expose immutable archive offsets plus server-owned evidence roles."""
    if not isinstance(text, str) or not text.strip():
        return []
    return [{'start': start, 'end': end, 'text': text[start:end], 'sectionRole': role,
        'citationRole': citation, 'eligibleForObjectMatch': role in _EVIDENCE_SECTION_ROLES,
        'structuralUnitKind': ('result_data_row' if role == 'result_data'
            and (_CAPTION.match(text[start:end]) or '|' in text[start:end]) else
            'guideline_recommendation_row' if role == 'guideline_recommendation'
            and len(text[start:end].splitlines()) == 1 else None),
        'allowedEvidenceUses': (['interpretation']
            if citation == 'secondary_background' or role in _CONTEXT_SECTION_ROLES
            else ['interpretation', 'action']) if role in _EVIDENCE_SECTION_ROLES else []}
        for start, end, role, citation in _original_passage_roles(text)]


def evaluation_passage_role(text, start, end):
    """A mixed/excluded span cannot acquire permission from a nearby body."""
    spans = [row for row in evaluation_original_passages(text)
        if row['start'] < end and row['end'] > start]
    eligible = bool(spans) and all(row['eligibleForObjectMatch'] for row in spans)
    secondary = any(row['citationRole'] == 'secondary_background' for row in spans)
    roles = {row['sectionRole'] for row in spans}
    citations = {row['citationRole'] for row in spans}
    return {'sectionRole': next(iter(roles)) if eligible and len(roles) == 1 else
            'primary_body' if eligible else 'excluded_or_mixed_source_structure',
        'citationRole': 'secondary_background' if secondary else
            'guideline_recommendation_with_supporting_references' if
            'guideline_recommendation_with_supporting_references' in citations else 'publication_prose',
        'eligibleForObjectMatch': eligible,
        'allowedEvidenceUses': (['interpretation'] if secondary or roles.intersection(_CONTEXT_SECTION_ROLES)
            else ['interpretation', 'action']) if eligible else []}


def evaluation_source_packet(record, *, query='', max_chars=12000, selected_quotes=()):
    """Select exact contextual passages; never call a selection full text.

    The complete available original remains in ``evaluation_original_text``.
    A scope reviewer must inspect it when this bounded packet omits content.
    Population/design/eligibility language and adjacent paragraphs accompany
    topical passages; none is generated from highlights or AI summaries.
    """
    row = _plain(record)
    original = evaluation_original_text(record)
    supplied_summary = not original and isinstance(row.get('sourceText'), str)
    if supplied_summary:
        # Packet selection supplies no permission. Legacy/internal summaries
        # are still checked against the server's complete original whitelist
        # by the caller; summary/highlight fields are never used here.
        original = row['sourceText']
    official_body = (row.get('provider') == 'official_guideline'
        and original == row.get('evidenceText') and bool(original)
        and row.get('sourceEvidenceKey') == 'official-original:sha256:' + hashlib.sha256(original.encode()).hexdigest())
    kind = ('supplied_source_text' if supplied_summary else 'official_primary_body' if official_body else 'pmc_primary_body_and_abstract' if row.get('provider') == 'pmc'
        and re.fullmatch(r'PMC\d+', str(row.get('sourceId') or ''), re.I)
        else 'abstract' if has_substantive_original_text(row.get('abstract'))
        else 'provider_original_excerpt')
    budget = max(0, int(max_chars))
    paragraphs, fragment_groups = [], []
    for passage in evaluation_original_passages(original):
        if not passage['eligibleForObjectMatch']:
            continue
        text = passage['text']
        if budget and len(text) > budget:
            # PubMed AbstractText and a PMC paragraph can each be a single
            # long literal block. Use complete original sentences, retaining
            # absolute offsets; never send nothing merely for lack of blank
            # lines, or slice a sentence and call it a complete source.
            fragments = [(passage['start'] + sentence.start(), passage['start'] + sentence.end(), sentence.group())
                for sentence in re.finditer(r'\S.*?(?:[。！？]|[.!?][\"\u201d\u2019\)]*(?=\s|$)|\Z)', text, re.S)]
            if len(fragments) > 1:
                group = list(range(len(paragraphs), len(paragraphs) + len(fragments)))
                fragment_groups.append(group)
                paragraphs.extend(fragments)
                continue
        paragraphs.append((passage['start'], passage['end'], text))
    selected = set()
    length = 0

    def include(index, limit=None):
        nonlocal length
        if index in selected or not 0 <= index < len(paragraphs):
            return
        cost = len(paragraphs[index][2]) + (2 if selected else 0)
        if length + cost <= (budget if limit is None else limit):
            selected.add(index)
            length += cost
    focused = set()
    for quote in selected_quotes:
        if not isinstance(quote, str) or not quote:
            continue
        start = original.find(quote)
        if start < 0:
            continue
        focused.update(index for index, (left, right, _) in enumerate(paragraphs)
            if left < start + len(quote) and right > start)
    for index in sorted(focused):
        include(index)
    # A fragmented block retains its original introductory and limiting
    # sentences. Only the quote's own long paragraph is selected when a
    # focus exists; unrelated guideline branches remain separate.
    for group in fragment_groups:
        if not focused or focused.intersection(group):
            include(group[0])
            include(group[-1])
    for index in sorted(focused):
        for neighbour in (index - 1, index + 1):
            include(neighbour)
    scope = re.compile(r'\b(?:patient|participant|adult|child|pediatric|paediatric|pregnan|'
        r'eligib|exclud|includ|criteria|limitation|retrospective|observational|random|protocol|'
        r'insufficient|contraindicat|unless|only|except)\w*\b', re.I)
    for group in fragment_groups:
        if focused.intersection(group):
            contextual = [index for index in group if scope.search(paragraphs[index][2])]
            for index in sorted(contextual, key=lambda value: min(abs(value - focus) for focus in focused))[:4]:
                include(index)
    abstract = row.get('abstract') or ''
    abstract_start = original.find(abstract) if abstract else -1
    if abstract_start >= 0:
        # The complete short abstract remains useful context. A long abstract
        # follows the same bounded methods/result/limitation selection as a
        # long primary paragraph rather than consuming the budget up front.
        abstract_limit = budget if len(abstract) <= budget else length + max(0, budget - length) // 3
        for index, (start, end, _) in enumerate(paragraphs):
            if start < abstract_start + len(abstract) and end > abstract_start:
                include(index, abstract_limit)
    if not focused:
        # Fragmenting a long result must not displace a real limitations
        # paragraph that previously fitted beside the abstract. Reserve a
        # bounded portion for explicit limitations before topical ranking.
        limit_budget = length + max(0, budget - length) // 3
        for index, (_, _, text) in enumerate(paragraphs):
            if re.search(r'\blimitations?\b|(?:does?|do) not establish|not generali[sz]able|局限|限制', text, re.I):
                include(index, limit_budget)
                if len(text) < 80 and re.fullmatch(r'\s*(?:limitations?|局限性|研究限制)\s*[:：]?\s*', text, re.I):
                    include(index + 1, limit_budget)
    tokens = {term.casefold() for term in re.findall(r'[A-Za-z][A-Za-z0-9-]{2,}', str(query))}
    if not tokens:
        tokens = {term.casefold() for term in re.findall(r'[A-Za-z][A-Za-z0-9-]{2,}', str(row.get('title') or ''))}
    tokens -= {'and', 'the', 'for', 'with', 'review', 'guideline', 'clinical', 'human', 'management'}
    ranked = sorted(range(len(paragraphs)), key=lambda index: (
        -sum(token in paragraphs[index][2].casefold() for token in tokens), index))
    # Reserve context alongside a topical window. A long methods section
    # cannot consume the whole author packet before its relevant result.
    if not focused:
        context_limit = min(budget, length + max(0, budget - length) // 3)
        for index, (_, _, text) in enumerate(paragraphs):
            if scope.search(text):
                include(index, context_limit)
        for index in ranked:
            if tokens and not any(token in paragraphs[index][2].casefold() for token in tokens):
                continue
            for neighbour in (index, index - 1, index + 1):
                include(neighbour)
        for index, (_, _, text) in enumerate(paragraphs):
            if scope.search(text):
                include(index)
        for index in range(len(paragraphs)):
            include(index)
    passages = [{'start': paragraphs[index][0], 'end': paragraphs[index][1],
        'text': paragraphs[index][2],
        **evaluation_passage_role(original, paragraphs[index][0], paragraphs[index][1])} for index in sorted(selected)]
    selected_text = '\n\n'.join(passage['text'] for passage in passages)
    complete = selected_text == original and bool(original)
    return {'text': selected_text, 'selectedSourceText': selected_text, 'selectedPassages': passages,
        'sourceOffsets': [{'start': passage['start'], 'end': passage['end']} for passage in passages],
        'sourceKind': kind, 'fullSourceLength': len(original),
        'selectedSourceLength': len(selected_text),
        'sourceCoverage': sum(passage['end'] - passage['start'] for passage in passages) / len(original) if original else 0,
        'fullSourceSha256': hashlib.sha256(original.encode()).hexdigest(),
        'isFullSource': complete, 'isFullPublicationBody': complete and kind in {'pmc_primary_body_and_abstract', 'official_primary_body'},
        'requiresFullSourceScopeReview': not complete,
        'selectionInstruction': '所示均为原文连续段；省略内容仍保存在完整原文，不能将这些选段称为全文。'
            '个体适用性须核对完整研究对象、入组/排除、限制条件与原句，不从标题或AI摘要推断。'}


def evaluation_publication_identifiers(record):
    """Normalize explicit document identifiers without guessing from titles."""
    row = _plain(record)
    doi = unicodedata.normalize('NFKC', str(row.get('doi') or '')).strip().casefold()
    doi = re.sub(r'^https?://(?:dx\.)?doi\.org/|^doi:\s*', '', doi).strip()
    if not doi:
        url = str(row.get('url') or '').strip()
        match = re.fullmatch(r'https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/[^\s<>?#]+)(?:[?#].*)?', url, re.I)
        doi = match[1].casefold() if match else ''
    pmid = None
    for value in (row.get('url'), row.get('sourceId')):
        value = str(value or '').strip()
        match = re.match(r'^(?:https?://)?(?:www\.)?pubmed\.ncbi\.nlm\.nih\.gov/(\d+)(?:[/?#]|$)',
            value, re.I) or re.fullmatch(r'pubmed:(\d+)', value, re.I)
        if match:
            pmid = match[1]
            break
    return doi, pmid


def _unbound_publication_identity(record):
    row = _plain(record)
    title = re.sub(r'\W+', '', unicodedata.normalize('NFKC', str(row.get('title') or '')).casefold())
    parent = row.get('parentGuideId')
    if (row.get('category') == 'guideline' and row.get('provider') == 'evimed_v2'
            and not parent and isinstance(row.get('sourceId'), str) and row['sourceId'].strip()):
        # A provider metadata row and its literal block share the same
        # document ID. This is bibliography identity only: the metadata row
        # still fails is_trusted_guide_block and supplies no original text.
        parent = row['sourceId']
    if row.get('category') == 'guideline' and parent:
        return ('guide', row.get('provider'), parent, row.get('year'))
    doi, pmid = evaluation_publication_identifiers(row)
    if doi:
        return ('doi', doi)
    if pmid:
        return ('pmid', pmid)
    return ('title', row.get('category'), title, row.get('year'))


_PUBLICATION_ORIGIN_TOKEN = object()


def _publication_source_fingerprint(record):
    row = _plain(record)
    # Query/target associations and the bibliography's re-keying do not
    # change a source. Every original/citation metadata field does.
    fields = ('provider', 'sourceId', 'sourceEvidenceKey', 'parentGuideId',
        'category', 'title', 'source', 'year', 'authors', 'studyTypes',
        'language', 'url', 'doi', 'abstract', 'evidenceText', 'sourceText')
    return hashlib.sha256(json.dumps({key: row.get(key) for key in fields},
        ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class _PublicationOriginBinding:
    identity: tuple
    origin_fingerprint: str
    hydrated_fingerprint: str
    origin_record: object = field(repr=False, compare=False)
    _token: object = field(repr=False, compare=False)

    def __deepcopy__(self, memo):
        # A copy remains attached to its actual request-local origin, not a
        # fabricated serialized snapshot of that origin's metadata.
        return self


def bind_hydrated_evaluation_publication(existing, hydrated):
    """Group a verified supplemental original with its actual source edition.

    This private in-process binding affects bibliography identity only. It
    grants no original-text permission or scientific/applicability approval.
    Both records keep their independent literal text and provenance.
    """
    from .schemas import EvidenceRecord
    if (not isinstance(existing, EvidenceRecord) or not isinstance(hydrated, EvidenceRecord)
            or existing is hydrated or not hydrated_original_matches_publication(existing, hydrated)):
        return False
    _bind_publication_origin(existing, hydrated)
    return True


def _bind_publication_origin(existing, hydrated):
    binding = _PublicationOriginBinding(_unbound_publication_identity(existing),
        _publication_source_fingerprint(existing), _publication_source_fingerprint(hydrated),
        existing, _PUBLICATION_ORIGIN_TOKEN)
    object.__setattr__(hydrated, '_evaluation_publication_origin_binding', binding)


def _verified_publication_origin(record):
    binding = getattr(record, '_evaluation_publication_origin_binding', None)
    if (isinstance(binding, _PublicationOriginBinding) and binding._token is _PUBLICATION_ORIGIN_TOKEN
            and binding.hydrated_fingerprint == _publication_source_fingerprint(record)
            and binding.origin_fingerprint == _publication_source_fingerprint(binding.origin_record)
            and binding.identity == _unbound_publication_identity(binding.origin_record)):
        return binding
    return None


def evaluation_publication_identity(record):
    """Identify the verified edition while retaining the source record."""
    binding = _verified_publication_origin(record)
    return binding.identity if binding else _unbound_publication_identity(record)


def hydrated_original_matches_publication(existing, hydrated):
    """Bind supplemental source text to its requested publication/edition."""
    left, right = _plain(existing), _plain(hydrated)
    left_doi, left_pmid = evaluation_publication_identifiers(left)
    right_doi, right_pmid = evaluation_publication_identifiers(right)
    if left_doi and right_doi and left_doi != right_doi:
        return False
    if left_pmid and right_pmid and left_pmid != right_pmid:
        return False
    if left_doi and left_doi == right_doi or left_pmid and left_pmid == right_pmid:
        return True
    def title(row):
        return re.sub(r'\W+', '', unicodedata.normalize('NFKC', str(row.get('title') or '')).casefold())
    # An unqualified same title may describe different editions. Missing
    # year/identifier evidence cannot silently authorize a replacement.
    return bool(title(left) and title(left) == title(right) and left.get('year') is not None
        and right.get('year') is not None and str(left['year']) == str(right['year']))


def _guideline_edition_keys(record):
    row = _plain(record)
    doi, pmid = evaluation_publication_identifiers(row)
    pmc = re.match(r'https?://(?:pmc\.ncbi\.nlm\.nih\.gov|www\.ncbi\.nlm\.nih\.gov/pmc)/articles/'
        r'(PMC\d+)(?:[/?#]|$)', str(row.get('url') or ''), re.I)
    keys = [(field, value) for field, value in (('doi', doi), ('pmid', pmid),
        ('pmcid', pmc[1].upper() if pmc else None)) if value]
    title = unicodedata.normalize('NFKC', str(row.get('title') or '')).strip().casefold()
    edition = re.search(r'\(((?:19|20)\d{2})\s*(?:年|edition|update|version)?\)$', title)
    year = row.get('year')
    normalized = re.sub(r'\W+', '', title)
    if len(normalized) >= 18 and (year or edition) and (not edition or not year or str(year) == edition[1]):
        keys.append(('dated_title', normalized, str(year or edition[1])))
    return keys


def _citation_metadata_preference(record):
    return (bool(evaluation_publication_identifiers(record)[0]), record.provider != 'evimed_v2',
        record.year is not None, bool(record.authors), bool(record.url))


def reconcile_evaluation_guideline_representations(records):
    """Identify one edition through shared IDs or a full, dated title.

    Same-site mirrors and translated metadata can bridge literal library
    blocks to one publication. Independent blocks remain in the source
    store, and conflicting explicit publication identifiers never merge.
    """
    groups, by_key, roots, identifiers = {}, {}, {}, {}
    for record in records:
        if record.category != 'guideline' or not evaluation_original_text(record):
            continue
        identity = evaluation_publication_identity(record)
        groups.setdefault(identity, []).append(record)
        roots[identity] = identity
        for key in _guideline_edition_keys(record):
            by_key.setdefault(key, {}).setdefault(identity, None)
            if key[0] != 'dated_title':
                identifiers.setdefault(identity, {}).setdefault(key[0], set()).add(key[1])

    def root(identity):
        while roots[identity] != identity:
            identity = roots[identity]
        return identity

    # Trusted identifiers precede title bridges. Do not allow a chain of
    # aliases to cross different editions with explicit contradictory IDs.
    for key, identities in sorted(by_key.items(), key=lambda item: item[0][0] == 'dated_title'):
        cluster = list(dict.fromkeys(root(identity) for identity in identities))
        if len(cluster) < 2:
            continue
        merged = {}
        for identity in cluster:
            for kind, values in identifiers.get(identity, {}).items():
                merged.setdefault(kind, set()).update(values)
        if (any(len(values) > 1 for values in merged.values())
                or key[0] == 'dated_title' and (not merged or len(cluster) != 2)):
            continue
        anchor = cluster[0]
        identifiers[anchor] = merged
        for identity in cluster[1:]:
            roots[identity] = anchor

    representations = {}
    for identity, group in groups.items():
        representations.setdefault(root(identity), []).append(group)
    for cluster in representations.values():
        if len(cluster) < 2:
            continue
        members = [record for group in cluster for record in group]
        anchor = max(members, key=_citation_metadata_preference)
        for record in members:
            if record is not anchor:
                _bind_publication_origin(anchor, record)


@dataclass(frozen=True)
class EvaluationOriginalPartition:
    existing: tuple
    candidates: tuple
    quarantined: tuple
    conflict_keys: tuple[str, ...]
    gaps: tuple[dict, ...]


def _publication_records_conflict(left, right):
    a, b = _plain(left), _plain(right)
    left_doi, left_pmid = evaluation_publication_identifiers(a)
    right_doi, right_pmid = evaluation_publication_identifiers(b)
    if (left_doi and right_doi and left_doi != right_doi
            or left_pmid and right_pmid and left_pmid != right_pmid):
        return True
    left_binding = _verified_publication_origin(left)
    right_binding = _verified_publication_origin(right)
    left_identity = left_binding.identity if left_binding else _unbound_publication_identity(left)
    right_identity = right_binding.identity if right_binding else _unbound_publication_identity(right)
    if left_identity != right_identity:
        return True
    if ((left_binding and left_binding.origin_record is right)
            or (right_binding and right_binding.origin_record is left)
            or (left_binding and right_binding and left_binding.origin_record is right_binding.origin_record)):
        return False
    def title(row):
        return re.sub(r'\W+', '', unicodedata.normalize('NFKC', str(row.get('title') or '')).casefold())
    shared_identifier = bool(left_doi and left_doi == right_doi or left_pmid and left_pmid == right_pmid)
    if not shared_identifier and title(a) and title(b) and title(a) != title(b):
        return True
    if (a.get('provider') == b.get('provider') and a.get('sourceId') and b.get('sourceId')
            and a['sourceId'] != b['sourceId'] and not shared_identifier):
        return True
    return False


def partition_evaluation_originals(existing, candidates=()):
    """Quarantine an entire conflicting semantic key, retaining other sources.

    The caller must replace its old pool with ``existing`` as well as filter
    candidates; a new-record list alone cannot remove a bad old record. The
    untouched quarantined objects and private gaps retain the provenance
    problem for audit. No row, original-text permission or input is changed.
    """
    old, new = tuple(existing), tuple(candidates)
    keyed, publications = {}, {}
    for record in (*old, *new):
        key = _plain(record).get('evidenceKey')
        if not isinstance(key, str) or not key:
            continue
        keyed.setdefault(key, []).append(record)
        publications.setdefault(evaluation_publication_identity(record), []).append(record)
    conflicts = set()
    # The second grouping catches contradictory DOI/PMID metadata behind one
    # provider document even when different query keys name that document.
    for group in (*keyed.values(), *publications.values()):
        if any(_publication_records_conflict(left, right)
                for index, left in enumerate(group) for right in group[index + 1:]):
            conflicts.update(_plain(record)['evidenceKey'] for record in group)
    def bad(record):
        return _plain(record).get('evidenceKey') in conflicts
    quarantined = tuple(record for record in (*old, *new) if bad(record))
    keys = tuple(sorted(conflicts))
    gaps = tuple({'missingRole': 'evidence_provenance', 'reason': 'semantic_key_publication_conflict',
        'evidenceKey': key, 'recordCount': sum(_plain(record).get('evidenceKey') == key
            for record in quarantined)} for key in keys)
    return EvaluationOriginalPartition(tuple(record for record in old if not bad(record)),
        tuple(record for record in new if not bad(record)), quarantined, keys, gaps)


def _require_unambiguous_originals(existing, candidates=()):
    if partition_evaluation_originals(existing, candidates).conflict_keys:
        raise ValueError('one semantic evidence key maps to different publications')


def retain_new_evaluation_originals(existing, candidates):
    """Return only new literal originals, without mutating either input.

    Repeated publication/text pairs coalesce across semantic query keys;
    whitespace-only differences do not create new evidence. A semantic key
    may retain different originals of the same publication, but mapping that
    key to a different publication is an explicit provenance error.
    """
    existing, candidates = tuple(existing), tuple(candidates)
    _require_unambiguous_originals(existing, candidates)
    publications, originals = {}, set()

    def inspect(record):
        row = _plain(record)
        text = evaluation_original_text(row)
        if not text:
            return None  # Bibliography metadata is not a retained original.
        identity = evaluation_publication_identity(record)
        key = row.get('evidenceKey')
        if isinstance(key, str) and key:
            if key in publications and publications[key] != identity:
                raise ValueError('one semantic evidence key maps to different publications')
            publications[key] = identity
        return (identity, ' '.join(text.split())) if text else None

    for record in existing:
        if signature := inspect(record):
            originals.add(signature)
    retained = []
    for record in candidates:
        signature = inspect(record)
        if signature is None or signature in originals:
            continue
        originals.add(signature)
        retained.append(record)
    return retained


def evaluation_citation_records(records):
    """Return publication representatives plus exact semantic-key mapping."""
    records = tuple(records)
    _require_unambiguous_originals(records)
    publications, keys = {}, {}
    for record in records:
        identity = evaluation_publication_identity(record)
        key = 'evaluate-publication-' + hashlib.sha256(repr(identity).encode()).hexdigest()[:32]
        if record.evidence_key in keys and keys[record.evidence_key] != key:
            raise ValueError('one semantic evidence key maps to different publications')
        keys[record.evidence_key] = key
        if key not in publications:
            publications[key] = record.model_copy(deep=True, update={'evidence_key': key})
        else:
            existing = publications[key]
            if _citation_metadata_preference(record) > _citation_metadata_preference(existing):
                for field in ('provider', 'title', 'source', 'year', 'authors', 'study_types', 'url', 'doi'):
                    setattr(existing, field, deepcopy(getattr(record, field)))
            existing.relevance = max(existing.relevance, record.relevance)
            for field in ('supports', 'query_ids', 'microbe_ids', 'disease_ids', 'intents', 'targets'):
                values = getattr(existing, field)
                values.extend(value for value in getattr(record, field) if value not in values)
    return list(publications.values()), keys


def build_evaluation_evidence_summaries(records, numbers):
    """Group literal blocks under their existing bibliography number.

    Blocks retain independent provenance. Joining with paragraph separators
    does not turn them into a fabricated abstract or an approved claim.
    Every resulting sentence still requires the two original-text reviews.
    """
    records = tuple(records)
    _require_unambiguous_originals(records)
    grouped = {}
    for record in records:
        row = _plain(record)
        text = evaluation_original_text(row)
        number = numbers.get(row.get('evidenceKey'))
        if number is None:
            continue
        identity = evaluation_publication_identity(record)
        if number not in grouped:
            grouped[number] = (identity, deepcopy(row), [])
        previous_identity, combined, blocks = grouped[number]
        if previous_identity != identity:
            raise ValueError('one evidence number maps to different original documents')
        for key in ('supports', 'microbeIds', 'diseaseIds', 'intents', 'targets', 'relationBindings'):
            values = combined.setdefault(key, [])
            for value in row.get(key, []):
                if value not in values:
                    values.append(deepcopy(value))
        if text:
            block = {key: deepcopy(row.get(key)) for key in
                ('evidenceKey', 'provider', 'sourceId', 'parentGuideId', 'sourceEvidenceKey',
                 'supports', 'queryIds', 'microbeIds', 'diseaseIds', 'intents', 'targets', 'relationBindings')}
            block['text'] = text
            block['modelSourcePacket'] = evaluation_source_packet(record,
                query=' '.join(str(value) for value in row.get('targets', [])))
            if block not in blocks:
                blocks.append(block)
    result = []
    for number, (_, row, blocks) in grouped.items():
        original = '\n\n'.join(dict.fromkeys(block['text'] for block in blocks))
        result.append({
            'number': number, 'category': row.get('category'), 'title': row.get('title'),
            'summary': original, 'sourceText': original,
            'originalTextBlocks': blocks,
            'modelSourcePackets': [{'evidenceKey': block.get('evidenceKey'),
                **block['modelSourcePacket']} for block in blocks],
            'evidenceKey': row.get('evidenceKey'), 'year': row.get('year'),
            'doi': row.get('doi'), 'url': row.get('url'),
            **{field: row.get(field, []) for field in
                ('supports', 'microbeIds', 'diseaseIds', 'relationBindings', 'intents', 'targets')},
            'interpretation': row.get('interpretation'),
        })
    return result
