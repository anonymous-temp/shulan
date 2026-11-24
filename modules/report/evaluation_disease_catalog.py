"""Source-owned model directories, without public input flags or patient reuse."""
from __future__ import annotations

import re
import unicodedata

from . import evaluation_catalog as catalog


def _get(value, snake, camel=None, default=None):
    return value.get(snake, value.get(camel, default)) if isinstance(value, dict) else getattr(value, snake, default)


def _source_family(text):
    text = str(text or '')
    parts = re.split(r'=====\s*PAGE\s+(\d+)\s*=====', text)
    cover = parts[2] if len(parts) > 2 and parts[1] == '1' else text[:1600]
    compact = lambda value: re.sub(r'\s+', '', value)
    title, whole = compact(cover), compact(text)
    # Newer originals draw the whole cover as an image, so page 1 carries no
    # extractable title. The method is then read from the printed "检测技术"
    # field of the sample-information page; the directory itself is still
    # confirmed below by the complete numeric risk table and chapter numbers.
    if '宏基因组测序' not in title and not re.search(r'检测技术[:：]高通量宏基因组测序', whole):
        return None
    if ('儿童肠道菌群检测报告' in title
            and '肠道菌群发育成熟度相关菌群' in whole and '影响儿童生长指标' in whole):
        return 'G1'
    if ('肠道微生态菌群精准检测报告' in title and '专业版' in title
            and '儿童肠道菌群检测报告' not in title and '肠道年龄评估' in whole):
        return 'GM'
    # The supplied newer covers draw the report title as an image. Recognise
    # their directory only with this original's complete numeric risk table;
    # age, a filename or child wording in an AI paragraph supply no identity.
    normalized = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    normalized = normalized.replace('II型糖尿病', '2型糖尿病')
    signatures = set()
    model_names = {name for rows in catalog.SOURCE_DISEASE_CATALOGS.values() for _, _, name in rows}
    numeric = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)'
    for match in re.finditer('疾病类型风险值检测结果', normalized):
        table = normalized[match.end():match.end() + 900]
        actual = {name for name in model_names if re.search(re.escape(name)
            + numeric + r'(?:低风险|中低风险|中风险|中高风险|高风险)', table)}
        for family in ('GM', 'G1'):
            expected = {name for _, _, name in catalog.SOURCE_DISEASE_CATALOGS[family]}
            if actual == expected:
                signatures.add(family)
    child_directory = all(re.search(pattern, normalized) for pattern in (
        r'(?<![\d.])1\.8肠道菌群发育成熟度相关菌群',
        r'(?<![\d.])1\.9影响儿童生长指标相关肠道菌群'))
    adult_directory = all(re.search(pattern, normalized) for pattern in (
        r'(?<![\d.])1\.2肠道年龄评估', r'(?<![\d.])2\.5抗生素耐药基因',
        r'(?<![\d.])3\.21帕金森综合征'))
    if signatures == {'G1'} and child_directory:
        return 'G1'
    if signatures == {'GM'} and adult_directory:
        return 'GM'
    return None


def attach_source_catalog(parsed, document):
    """Confirm the selected PDF's actual cover/layout, never its user's age."""
    if (document is None or parsed.source_name != document.filename or not parsed.source_checksum
            or parsed.source_checksum != document.metadata.get('content_sha256')):
        return parsed
    family = _source_family(document.text)
    if family is None:
        return parsed
    output = parsed.model_copy(deep=True)
    title = '儿童肠道菌群检测报告' if family == 'G1' else '肠道微生态菌群精准检测报告'
    markers = ('肠道菌群发育成熟度相关菌群\n影响儿童生长指标' if family == 'G1' else '专业版\n肠道年龄评估')
    if not output.source_summary:
        output.source_summary = title + '\n【宏基因组测序】\n' + markers
    output._evaluation_source_audit = [row for row in parsed._evaluation_source_audit
        if row.get('kind') != 'source_disease_catalog'] + [dict(kind='source_disease_catalog',
            family=family, sourceFile=document.filename, sourceChecksum=parsed.source_checksum,
            sourceTitle=title, method='宏基因组测序')]
    return output


def initialize_source_catalog(data, parsed):
    """Keep the known source template in the existing C version slot."""
    family, version = None, None
    for entry in getattr(parsed, '_evaluation_source_audit', []):
        if entry.get('sourceFile') != parsed.source_name:
            continue
        if parsed.source_checksum and entry.get('sourceChecksum') != parsed.source_checksum:
            continue
        if entry.get('kind') == 'source_disease_catalog' and entry.get('family') in {'GM', 'G1'}:
            family = version = entry['family']
            break
        if (entry.get('kind') == 'template_json_field_mapping'
                and entry.get('family') in {'GM', 'G1', 'GS.meta'}):
            family = entry['family']
            version = entry.get('templateNumber')
            continue
        if (entry.get('kind') == 'template_pdf_mapping'
                and entry.get('family') in {'GM', 'G1', 'GS.meta'}):
            family = entry['family']
            version = entry.get('templateNumber') or family
            continue
        if (entry.get('kind') == 'template_json_field_mapping'
                and entry.get('recognizedShape') == 'gs-meta-jun-jkj-yingyang2'):
            family = 'GS.meta'
            version = entry.get('templateNumber')
    if family is None:
        family = _source_family(parsed.source_summary)
        version = family
    if family is not None:
        object.__setattr__(data, '_source_disease_catalog_family', family)
        source = next((row for row in data.introduction.sources if row.source_code == 'C'), None)
        if source is not None and source.version is None and version:
            source.version = version
    return family


def source_catalog_family(data):
    """Survive public JSON/GET roundtrips without restoring the old 30 rows."""
    private = getattr(data, '_source_disease_catalog_family', None)
    if private in catalog.SOURCE_DISEASE_CATALOGS:
        return private
    introduction = _get(data, 'introduction', default={})
    versions = [_get(row, 'version') for row in _get(introduction, 'sources', default=[])
        if _get(row, 'source_code', 'sourceCode') == 'C']
    # A completed directory is self-describing even when an older caller
    # supplied its own version string. This identifies its item set, not age
    # or a provider, and never copies a value from another source.
    risk = _get(data, 'disease_risk_assessment', 'diseaseRiskAssessment', {})
    observed = {code for category in _get(risk, 'categories', default=[])
        for row in _get(category, 'items', default=[])
        if not str(_get(row, 'code') or '').startswith('nativeAssociation:')
        if (code := catalog.fixed_disease_code(_get(row, 'code'))
            or catalog.fixed_disease_code(_get(row, 'name')))}
    from .evaluation_template_input import TEMPLATE_DIRECTORY_FAMILIES
    version_families = {'GM': 'GM', 'G1': 'G1', **TEMPLATE_DIRECTORY_FAMILIES}
    version_family = next((version_families[value]
        for value in versions if value in version_families), None)
    # Display dimensions may change without changing the source directory.
    adult = {code for _, code, _ in catalog.SOURCE_DISEASE_CATALOGS['GM']}
    child = {code for _, code, _ in catalog.SOURCE_DISEASE_CATALOGS['G1']}
    gs_source = {code for _, code, _ in catalog.SOURCE_DISEASE_CATALOGS['GS.meta']}
    if observed == gs_source:
        return 'GS.meta'
    if observed == adult:
        # GM and GS.meta share the adult display directory. A truthful adult
        # version disambiguates the source; a conflicting child version must
        # not replace the already adopted adult questions on a later read.
        return version_family if version_family in {'GM', 'GS.meta'} else ('GS.meta' if observed == gs_source else 'GM')
    if observed == child:
        return 'G1'
    if version_family is not None:
        return version_family
    return None
