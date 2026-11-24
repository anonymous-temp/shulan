import json

import pytest
from pydantic import ValidationError

from adapters.gut_barrier.sources import _bounded_json
from modules.gut_barrier.input_limits import normalize_structured_source
from modules.gut_barrier.schemas import GutBarrierRequest


def request(value, key="microbiomeData"):
    return GutBarrierRequest.model_validate({"userId": "limits", "sessionId": "test", key: value})


def test_thousand_species_inline_and_downloaded_have_identical_normalization():
    value = {"species": [{"name": f"species-{i}", "abundance": i / 1000} for i in range(1000)],
             "externalId": 2 ** 80}
    inline = request(value).microbiome_data
    downloaded = _bounded_json(json.dumps(value).encode())
    assert inline == downloaded
    assert value["externalId"] == 2 ** 80  # No mutation of the caller's object.
    assert inline["externalId"] == str(2 ** 80)
    with pytest.raises(ValidationError):
        request(value, "healthSurvey")  # Do not widen the unrelated survey contract.


@pytest.mark.parametrize("value", [
    {"species": [None] * 30000},
    {"notes": "x" * 20001},
    {"x" * 201: 1},
    {"value": float("nan")},
    {"value": float("inf")},
])
def test_inline_and_downloaded_reject_same_invalid_documents(value):
    with pytest.raises(ValueError):
        request(value)
    with pytest.raises(ValueError):
        _bounded_json(json.dumps(value).encode())


def test_exact_complexity_boundaries_and_byte_budget():
    normalize_structured_source({"values": [None] * 29998})  # 30,000 nodes.
    with pytest.raises(ValueError):
        normalize_structured_source({"values": [None] * 29999})
    value = None
    for _ in range(20):
        value = {"child": value}
    normalize_structured_source(value)
    with pytest.raises(ValueError):
        normalize_structured_source({"child": value})
    with pytest.raises(ValueError, match="exceeds limit"):
        normalize_structured_source({"values": ["界" * 20000] * 40})


def test_null_and_falsy_values_preserved_and_non_json_values_rejected():
    assert request(None).microbiome_data is None
    value = {"count": 0, "flag": False, "missing": None, "rows": []}
    assert request(value).microbiome_data == value
    with pytest.raises(ValueError):
        normalize_structured_source({"not_json": {1, 2}})
