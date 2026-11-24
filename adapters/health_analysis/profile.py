"""Translate Profile's public read result without reaching into its stores."""

import math
from copy import deepcopy

from health_profile.document_archive_contracts import ProfileDocumentArchiveChangedError
from health_profile.document_archive_contracts import ProfileDocumentArchiveReadError
from health_profile.document_archive_contracts import ProfileReportLifecycleConflict
from modules.health_analysis.basic_info_sync import BasicInfoConflictError
from modules.health_analysis.contracts import AnalysisCapacityError
from modules.health_analysis.contracts import SourceUnavailableError

_CONTEXT_FIELDS = {
    'age': 'age',
    'patientage': 'age',
    '年龄': 'age',
    'ageyears': 'ageYears',
    'agemonths': 'ageMonths',
    'agedays': 'ageDays',
    'ageunit': 'ageUnit',
    'lifestage': 'lifeStage',
    'physiologicalstage': 'physiologicalStage',
    '生理阶段': 'physiologicalStage',
    'specialstage': 'specialStage',
    '特殊阶段': 'specialStage',
    'pregnancystatus': 'pregnancyStatus',
    '妊娠状态': 'pregnancyStatus',
    '孕期状态': 'pregnancyStatus',
    'lactationstatus': 'lactationStatus',
    '哺乳状态': 'lactationStatus',
    'pregnant': 'pregnant',
    '是否妊娠': 'pregnant',
    'lactating': 'lactating',
    'breastfeeding': 'breastfeeding',
    '是否哺乳': 'breastfeeding',
    'gestationalweeks': 'gestationalWeeks',
    'pregnancyweeks': 'pregnancyWeeks',
    '孕周': 'gestationalWeeks',
    'gestationalage': 'gestationalAge',
}
_CONTEXT_CONTAINERS = frozenset({'subject', 'basicInfo', 'patientInfo', 'subjectInfo', 'patient', 'demographics'})


def _pointer_key(value):
    return value.replace('~', '~0').replace('/', '~1')


def _report_context(result, observation_pointer):
    """Copy explicit medical background, with its exact fixed-result pointer.

    Only root and the observation's actual section ancestors are considered.
    A different section, family member, processing state or report gender must
    never supply the patient's applicability context by proximity or guessing.
    """
    nodes = [(result, '')]
    parts = observation_pointer.split('/') if isinstance(observation_pointer, str) else []
    node, prefix, index = result, '', 1
    while index + 1 < len(parts) and parts[index] == 'sections' and parts[index + 1].isdecimal():
        sections, section_index = node.get('sections'), int(parts[index + 1])
        if (
            not isinstance(sections, list)
            or section_index >= len(sections)
            or not isinstance(sections[section_index], dict)
        ):
            break
        prefix += '/sections/' + parts[index + 1]
        node = sections[section_index]
        nodes.append((node, prefix))
        index += 2
    contexts = []
    for node, prefix in nodes:
        containers = [(node, prefix)]
        containers.extend(
            (value, prefix + '/' + _pointer_key(key))
            for key, value in node.items()
            if key in _CONTEXT_CONTAINERS and isinstance(value, dict)
        )
        for container, pointer in containers:
            for key, value in container.items():
                normalized_key = key.replace('_', '').replace('-', '').casefold()
                field = _CONTEXT_FIELDS.get(normalized_key)
                if field is None or not isinstance(value, (str, int, float, bool)):
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                if field in {'age', 'ageYears', 'ageMonths', 'ageDays'} and isinstance(value, bool):
                    continue
                contexts.append({'field': field, 'value': value, 'pointer': pointer + '/' + _pointer_key(key)})
    return contexts


class ProfileMaterialsAdapter:
    def __init__(self, reader):
        self.reader = reader

    async def read(self, scope):
        try:
            epoch = getattr(scope, 'lifecycle_epoch', None)
            target = {'user_id': scope.user_id, 'subject_id': scope.subject_id}
            if epoch is not None:
                target['expected_lifecycle_epoch'] = epoch
            result = await self.reader.read(**target)
        except OverflowError:
            raise AnalysisCapacityError('Profile report material capacity exceeded') from None
        except ProfileReportLifecycleConflict:
            raise BasicInfoConflictError('The fixed Profile subject or lifecycle changed') from None
        except (ProfileDocumentArchiveChangedError, ProfileDocumentArchiveReadError, TimeoutError):
            raise SourceUnavailableError('profile_read_unavailable') from None
        if not isinstance(result, dict) or not isinstance(result.get('materials'), list):
            raise TypeError('Profile material reader returned an invalid public result')
        selected = result.get('scope')
        if selected is None and result['materials']:
            raise TypeError('Nonempty Profile material requires an existing self scope')
        if selected is not None and (
            not isinstance(selected, dict)
            or not isinstance(selected.get('userId'), str)
            or not isinstance(selected.get('subjectId'), str)
            or not selected['subjectId']
        ):
            raise TypeError('Profile material reader returned an invalid scope')
        if selected is not None and selected['userId'] != scope.user_id:
            raise PermissionError('Profile material is outside the verified user scope')
        if selected is not None and (
            scope.subject_id is not None
            and selected['subjectId'] != scope.subject_id
            or epoch is not None
            and selected.get('lifecycleEpoch') != epoch
        ):
            raise BasicInfoConflictError('The fixed Profile subject or lifecycle changed')
        if result.get('status') == 'unavailable':
            raise SourceUnavailableError('profile_read_unavailable')
        frozen = deepcopy(result)
        self._attach_report_context(frozen)
        return frozen

    @staticmethod
    def _attach_report_context(result):
        selected = result.get('scope')
        if selected is None:
            return
        snapshots = result.get('sourceSnapshot') or {}
        if not isinstance(snapshots, dict):
            raise TypeError('Profile reader returned an invalid report snapshot')
        archives = snapshots.get('archives', [])
        if not isinstance(archives, (list, tuple)):
            raise TypeError('Profile reader returned an invalid archive list')
        fixed_results = {}
        for archive in archives:
            if not isinstance(archive, dict):
                raise TypeError('Profile reader returned an invalid archive snapshot')
            descriptor, raw = archive.get('descriptor'), archive.get('result')
            if not isinstance(descriptor, dict) or not isinstance(raw, dict):
                continue
            identity = descriptor.get('extraction_id')
            if (
                not isinstance(identity, str)
                or not identity
                or descriptor.get('user_id') != selected['userId']
                or descriptor.get('subject_id') != selected['subjectId']
                or descriptor.get('archive_status') != 'ready'
                or descriptor.get('source_status') in {'withdrawn', 'retired'}
            ):
                continue
            if identity in fixed_results and fixed_results[identity] != raw:
                raise TypeError('Profile snapshots disagree about one fixed extraction')
            fixed_results[identity] = raw
        for material in result['materials']:
            if not isinstance(material, dict):
                raise TypeError('Profile reader returned an invalid material')
            if material.get('kind') != 'report':
                continue
            metadata, content = material.get('metadata'), material.get('content')
            if not isinstance(metadata, dict):
                raise TypeError('Profile report material requires its fixed metadata')
            if content is None or content == '' or content == {} or content == []:
                continue
            snake, camel = metadata.get('extraction_id'), metadata.get('extractionId')
            if snake is not None and camel is not None and snake != camel:
                raise TypeError('Profile material has conflicting fixed extraction identities')
            identity = snake if snake is not None else camel
            raw = fixed_results.get(identity) if isinstance(identity, str) else None
            if raw is None:
                continue
            document_id = metadata.get('documentId')
            if document_id is not None and metadata.get('archiveDocumentId', document_id) != document_id:
                continue
            context = _report_context(raw, metadata.get('interpretationPointer', metadata.get('sourcePointer')))
            if not context:
                continue
            if isinstance(content, dict):
                content['reportContext'] = context
            else:
                material['content'] = {'observation': content, 'reportContext': context}
