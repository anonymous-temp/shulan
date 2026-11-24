"""Preview configuration never needs deployment credentials."""

import runpy
from pathlib import Path
from unittest.mock import patch

import pytest
from loongspears.utils.env import EnvConfigError

ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = {
    'HTTP_LOG_BODY_ENABLED': True,
    'HTTP_LOG_STRING_CHARS': 2000,
    'HTTP_LOG_LIST_ITEMS': 20,
    'HTTP_LOG_DICT_ITEMS': 50,
    'HTTP_LOG_DEPTH': 8,
    'HTTP_LOG_TOTAL_BYTES': 32768,
    'HTTP_LOG_PARSE_BYTES': 1048576,
}


def read_settings():
    with patch('loongspears.utils.env.load_env', return_value=None):
        return runpy.run_path(str(ROOT / 'config/env.py'))


def test_defaults_and_body_switch(monkeypatch):
    for name in DEFAULTS:
        monkeypatch.delenv(f'SHULAN_{name}', raising=False)
    settings = read_settings()
    assert {name: settings[name] for name in DEFAULTS} == DEFAULTS
    monkeypatch.setenv('SHULAN_HTTP_LOG_BODY_ENABLED', 'false')
    assert read_settings()['HTTP_LOG_BODY_ENABLED'] is False


@pytest.mark.parametrize(
    'name,value',
    [
        ('HTTP_LOG_DEPTH', '0'),
        ('HTTP_LOG_DEPTH', '33'),
        ('HTTP_LOG_STRING_CHARS', '0'),
        ('HTTP_LOG_LIST_ITEMS', '1'),
        ('HTTP_LOG_DICT_ITEMS', '-1'),
        ('HTTP_LOG_TOTAL_BYTES', '100'),
        ('HTTP_LOG_PARSE_BYTES', '0'),
    ],
)
def test_invalid_limits_fail_at_configuration_boundary(monkeypatch, name, value):
    monkeypatch.setenv(f'SHULAN_{name}', value)
    with pytest.raises(EnvConfigError, match=f'SHULAN_{name}'):
        read_settings()


@pytest.mark.parametrize(
    'filename', ['.env.example', '.env.example.dev', '.env.example.test', '.env.example.production']
)
def test_all_templates_document_preview_settings(filename):
    text = (ROOT / filename).read_text(encoding='utf-8')
    for name, value in DEFAULTS.items():
        encoded = str(value).lower() if isinstance(value, bool) else str(value)
        assert f'SHULAN_{name}={encoded}' in text
