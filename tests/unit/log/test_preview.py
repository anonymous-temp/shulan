"""Log previews remain bounded, readable, and independent of wire payloads."""

import copy
import json
from dataclasses import replace

import pytest

from utils.log.preview import PreviewLimits
from utils.log.preview import preview


def test_normal_values_and_sensitive_fields_do_not_mutate_input():
    value = {
        'text': '中文\n第二行',
        'values': [None, '', [], {}, True, 1, 1.5],
        'api_key': 'secret',
        'nested': {'accessToken': 'token'},
    }
    original = copy.deepcopy(value)
    output = preview(value)
    result = json.loads(output)
    assert value == original
    assert result['text'] == value['text']
    assert result['values'] == value['values']
    assert result['api_key'] == result['nested']['accessToken'] == '[REDACTED]'
    assert len(output.splitlines()) == 1
    assert '中文' in output


def test_string_keeps_both_ends_but_array_keeps_only_prefix():
    value = {'text': 'head' + 'x' * 2000 + 'tail', 'items': list(range(100))}
    result = json.loads(preview(value))
    assert result['text'].startswith('head')
    assert result['text'].endswith('tail')
    assert result['text'] == value['text'][:1000] + '***' + value['text'][-1000:]
    assert result['items'] == [*range(20), '***']


def test_large_dictionary_depth_and_circular_references():
    result = json.loads(preview(dict.fromkeys(map(str, range(100)), 1)))
    assert result == {**dict.fromkeys(map(str, range(50)), 1), '***': '***'}
    deep = {'a': {'b': {'c': [1, 2]}}}
    result = json.loads(preview(deep, replace(PreviewLimits(), depth=2)))
    assert result == {'a': {'b': '***'}}
    cyclic = []
    cyclic.append(cyclic)
    assert json.loads(preview(cyclic)) == ['***']
    shared = {'normal': True}
    assert json.loads(preview([shared, shared])) == [shared, shared]


@pytest.mark.parametrize('budget', [1024, 2048, 32768])
@pytest.mark.parametrize('text', ['中文😀\\"\n' * 2000, 'x' * 100000], ids=['unicode', 'ascii'])
def test_global_utf8_budget_including_keys_and_nested_containers(budget, text):
    limits = replace(PreviewLimits(), total_bytes=budget)
    value = {f'{index}{text}': [{'a': text, 'b': [text] * 100}] * 100 for index in range(100)}
    encoded = preview(value, limits)
    assert len(encoded.encode('utf-8')) <= budget
    assert isinstance(json.loads(encoded), dict)


def test_unsupported_values_do_not_invoke_repr_or_consume_iterators():
    class BadRepr:
        def __repr__(self):
            raise AssertionError('repr must not run')

    iterator = iter([1, 2])
    value = [BadRepr(), iterator, b'private-binary']
    output = preview(value)
    assert 'BadRepr' in output
    assert 'private-binary' not in output
    assert next(iterator) == 1


def test_all_unicode_line_separators_and_invalid_surrogates_stay_on_one_utf8_line():
    value = '\n\r\v\f\x85\u2028\u2029\ud800'
    output = preview(value)
    assert len(output.splitlines()) == 1
    assert json.loads(output) == value
    output.encode('utf-8')


def test_query_bytes_are_decoded_only_when_requested_and_secrets_masked():
    value = {'query': ['你好'.encode()], 'password': [b'hidden']}
    assert json.loads(preview(value, decode_bytes=True)) == {'query': ['你好'], 'password': '[REDACTED]'}


def test_traversal_does_not_walk_an_entire_large_container():
    class CountingDict(dict):
        def items(self):
            for index, pair in enumerate(super().items()):
                assert index < 50
                yield pair

    result = json.loads(preview(CountingDict.fromkeys(range(1000), 1)))
    assert result == {**dict.fromkeys(map(str, range(50)), 1), '***': '***'}


@pytest.mark.parametrize('count', [0, 1, 2, 3, 7])
def test_container_limit_retains_first_n_values_and_adds_marker_only_on_overflow(count):
    limits = replace(PreviewLimits(), list_items=2, dict_items=2)
    items = list(range(count))
    fields = {f'field{i}': i for i in items}
    original = copy.deepcopy([items, fields])
    assert json.loads(preview(items, limits)) == items[:2] + (['***'] if count > 2 else [])
    assert json.loads(preview(fields, limits)) == {
        **{f'field{i}': i for i in items[:2]},
        **({'***': '***'} if count > 2 else {}),
    }
    assert [items, fields] == original


@pytest.mark.parametrize('count', [31, 32, 33, 1000])
def test_string_boundary_preserves_short_values_and_uses_plain_middle_marker(count):
    value = ''.join(chr(0x4E00 + index) for index in range(count))
    result = json.loads(preview(value, replace(PreviewLimits(), string_chars=32)))
    assert result == (value if count <= 32 else value[:16] + '***' + value[-16:])


def test_dictionary_marker_does_not_overwrite_real_star_keys():
    value = {'***': 'original', '****': 2, 'hidden': 3}
    limits = replace(PreviewLimits(), dict_items=2)
    assert json.loads(preview(value, limits)) == {'***': 'original', '****': 2, '*****': '***'}
    # The marker must also avoid collisions with keys shortened for the preview.
    value = {'*' * 100: 'original', 'hidden': 3}
    limits = replace(limits, string_chars=32, dict_items=1)
    assert json.loads(preview(value, limits)) == {'*' * 35: 'original', '***': '***'}


def test_marker_collisions_and_total_budget_keep_json_valid_without_overwriting_fields():
    value = {'*' * count: count for count in range(3, 1003)}
    limits = replace(PreviewLimits(), dict_items=1000, string_chars=2000, total_bytes=32768)
    output = preview(value, limits)
    pairs = json.loads(output, object_pairs_hook=list)
    assert len(output.encode('utf-8')) <= limits.total_bytes
    assert len({key for key, _ in pairs}) == len(pairs)
    assert pairs[-1][1] == '***'
    assert all(value[key] == item for key, item in pairs[:-1])


def test_budget_omits_remaining_structure_with_plain_markers():
    limits = replace(PreviewLimits(), string_chars=10000, total_bytes=1024)
    value = {'text': '中' * 10000, 'later': {'value': 1}}
    output = preview(value, limits)
    result = json.loads(output)
    assert len(output.encode('utf-8')) <= 1024
    assert result['text'].count('***') == 1
    assert result['***'] == '***'
    assert 'later' not in result


def test_long_decoded_bytes_use_plain_middle_marker():
    value = ('前' + '中' * 10000 + '后').encode('utf-8')
    output = json.loads(preview(value, replace(PreviewLimits(), string_chars=32), decode_bytes=True))
    assert output.startswith('前') and output.endswith('后')
    assert output.count('***') == 1
    assert '省略' not in output and '字节' not in output
