"""Public Questionnaire capabilities; imports and construction perform no I/O."""

from .submitted_material import SUBMITTED_MATERIAL_SELECTION_V1
from .submitted_material import QuestionnaireSubmittedMaterialError
from .submitted_material import QuestionnaireSubmittedMaterialPort
from .submitted_material import QuestionnaireSubmittedMaterialReader

__all__ = [
    'SUBMITTED_MATERIAL_SELECTION_V1',
    'QuestionnaireSubmittedMaterialError',
    'QuestionnaireSubmittedMaterialPort',
    'QuestionnaireSubmittedMaterialReader',
    'create_application_questionnaire_submitted_material_reader',
]


def create_application_questionnaire_submitted_material_reader():
    from adapters.questionnaire_submitted_material import (
        create_application_questionnaire_submitted_material_reader as build,
    )

    return build()
