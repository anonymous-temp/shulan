"""Validated, immutable user-confirmed input; never inferred from clinical sources."""

from dataclasses import dataclass
from datetime import datetime

from modules.health_analysis.contracts import InvalidAnalysisInputError


@dataclass(frozen=True)
class ConfirmedBasicInfo:
    name: str
    age: int
    gender: str

    @classmethod
    def parse(cls, payload):
        name, age, gender = (payload.get(key) for key in ('name', 'age', 'gender'))
        if not isinstance(name, str) or not name.strip():
            raise InvalidAnalysisInputError('Invalid basic information')
        if type(age) is not int or not 0 <= age <= 150 or gender not in ('男', '女'):
            raise InvalidAnalysisInputError('Invalid basic information')
        return cls(name.strip(), age, gender)

    def values(self):
        return {'name': self.name, 'age': self.age, 'gender': self.gender}

    def model_context(self, confirmed_at: datetime):
        return {
            'ageYears': self.age,
            'gender': 'male' if self.gender == '男' else 'female',
            'confirmedAt': confirmed_at.isoformat(),
            'sourceReliability': 'very_high',
            'fieldPointers': {'ageYears': '/age', 'gender': '/gender'},
        }
