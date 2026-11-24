import runpy
from pathlib import Path

import pytest
from loongspears.utils.env import EnvConfigError

ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = {
    'LOG_FILE_ENABLED': True, 'LOG_MAX_BYTES': 52428800,
    'LOG_RETENTION_DAYS': 14, 'LOG_BACKUP_COUNT': 20,
    'LOG_USER_MAX_OPEN_FILES': 128, 'LOG_QUEUE_CAPACITY': 1000,
}


def test_file_defaults_and_relative_directory(monkeypatch):
    for name in (*DEFAULTS, 'LOG_DIR'):
        monkeypatch.delenv(f'SHULAN_{name}', raising=False)
    values = runpy.run_path(str(ROOT / 'config/env.py'))
    assert {key: values[key] for key in DEFAULTS} == DEFAULTS
    assert values['LOG_DIR'] == ROOT / 'logs'
    monkeypatch.setenv('SHULAN_LOG_DIR', 'custom-logs')
    assert runpy.run_path(str(ROOT / 'config/env.py'))['LOG_DIR'] == ROOT / 'custom-logs'


@pytest.mark.parametrize('name', list(DEFAULTS)[1:])
def test_invalid_file_limits(monkeypatch, name):
    monkeypatch.setenv(f'SHULAN_{name}', '0')
    with pytest.raises(EnvConfigError, match=name):
        runpy.run_path(str(ROOT / 'config/env.py'))


@pytest.mark.parametrize('name', ['', '.dev', '.production', '.test'])
def test_templates_include_file_config(monkeypatch, name):
    text = (ROOT / f'.env.example{name}').read_text(encoding='utf-8')
    settings = {}
    for line in text.splitlines():
        key, separator, value = line.strip().partition('=')
        if separator and key.startswith('SHULAN_LOG_'):
            settings[key] = value.strip()
    for key in (*DEFAULTS, 'LOG_DIR'):
        variable = f'SHULAN_{key}'
        assert variable in settings
        monkeypatch.setenv(variable, settings[variable])
    # Deployment examples may enable files; conftest independently isolates unit tests.
    enabled = settings['SHULAN_LOG_FILE_ENABLED'].lower()
    assert enabled in ('true', 'false')
    values = runpy.run_path(str(ROOT / 'config/env.py'))
    assert values['LOG_FILE_ENABLED'] is (enabled == 'true')
    for key in list(DEFAULTS)[1:]:
        assert values[key] == int(settings[f'SHULAN_{key}'])
