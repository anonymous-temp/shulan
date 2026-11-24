"""Questionnaire configuration contracts with no deployment files or network."""

from __future__ import annotations

import os
import runpy
from itertools import combinations
from pathlib import Path
from unittest.mock import patch

import pytest
from loongspears.utils.env import EnvConfigError

ROOT = Path(__file__).resolve().parents[3]
COLLECTION_DEFAULTS = {
    'MONGODB_COLL_QUESTIONNAIRES': 'record_questionnaires',
    'MONGODB_COLL_QUESTIONNAIRE_DOCUMENTS': 'questionnaire_documents',
    'MONGODB_COLL_QUESTIONNAIRE_CHUNKS': 'questionnaire_document_chunks',
    'MONGODB_COLL_QUESTIONNAIRE_FACTOR_TASKS': 'questionnaire_factor_tasks',
}
EXISTING_COLLECTIONS = (
    'MONGODB_COLL_SESSIONS',
    'MONGODB_COLL_HEALTH_REPORTS',
    'MONGODB_COLL_DOCUMENT_TRACES',
    'MONGODB_COLL_DOCUMENT_TRACE_CHUNKS',
    'MONGODB_COLL_DOCUMENT_INDEX',
    'MONGODB_COLL_DOCUMENT_EXTRACTIONS',
    'MONGODB_COLL_DOCUMENT_OBSERVATIONS',
    'MONGODB_COLL_HEALTH_REPORT_REVISIONS',
    'MONGODB_COLL_AGENT_LLM_CONTEXTS',
    'MONGODB_COLL_AGENT_CHECKPOINTS',
    'MONGODB_COLL_AGENT_CHECKPOINT_WRITES',
    'MONGODB_COLL_CHAT_CUSTOM_MESSAGE_OUTBOX',
    'MONGODB_COLL_HEALTH_SUBJECTS',
    'MONGODB_COLL_HEALTH_PROFILE_FACTS',
    'MONGODB_COLL_HEALTH_PROFILE_EVENTS',
    'MONGODB_COLL_HEALTH_PROFILE_VIEWS',
    'MONGODB_COLL_HEALTH_PROFILE_JOBS',
    'MONGODB_COLL_HEALTH_PROFILE_EVIDENCE_RESULTS',
    'MONGODB_COLL_REPORT_BATCHES',
    'MONGODB_COLL_REPORT_ARTIFACTS',
    'MONGODB_COLL_REPORT_CALLBACK_DELIVERIES',
)


@pytest.fixture
def read_settings():
    # Supply all required settings independently of conftest/process deployment
    # values. Reserved .invalid URLs pass production syntax validation without
    # ever contacting a real receiver, model, database, or file parser.
    safe_environment = {
        'SHULAN_ENVIRONMENT': 'test',
        'SHULAN_LOG_LEVEL': 'CRITICAL',
        'SHULAN_LOG_FILE_ENABLED': 'false',
        'SHULAN_LLM_API_KEY': 'synthetic-configuration-test-key',
        'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_ID': 'synthetic-docmind-id',
        'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_SECRET': 'synthetic-docmind-secret',
        'SHULAN_ALIYUN_DOCMIND_ENDPOINT': 'https://parser.example.invalid',
        'SHULAN_MONGODB_URI': 'mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=10',
        'SHULAN_MONGODB_DATABASE': 'questionnaire-config-fixture',
        'SHULAN_MONGODB_COLL_SESSIONS': 'fixture-sessions',
        'SHULAN_MONGODB_COLL_HEALTH_REPORTS': 'fixture-health-reports',
        'SHULAN_REPORT_CALLBACK_URL': 'https://receiver.example.invalid/callback',
        'SHULAN_REPORT_PRODUCT_URL': 'https://catalog.example.invalid/products',
    }

    def read(**settings):
        environment = dict(safe_environment)
        environment.update({f'SHULAN_{name}': value for name, value in settings.items()})
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                'loongspears.utils.env.load_env',
                return_value=None,
            ) as load_env,
        ):
            try:
                return runpy.run_path(str(ROOT / 'config' / 'env.py'))
            finally:
                load_env.assert_called_once_with(ROOT / '.env', override=False, required=False)

    return read


@pytest.mark.parametrize('environment', ['dev', 'test', 'production'])
@pytest.mark.parametrize('obsolete_value', [None, 'false', 'not-a-boolean'])
def test_questionnaire_configuration_has_no_api_switch(read_settings, environment, obsolete_value):
    values = {'ENVIRONMENT': environment}
    if obsolete_value is not None:
        values['QUESTIONNAIRE_ENABLED'] = obsolete_value
    settings = read_settings(**values)
    assert 'QUESTIONNAIRE_ENABLED' not in settings
    assert {name: settings[name] for name in COLLECTION_DEFAULTS} == COLLECTION_DEFAULTS
    assert settings['REPORT_CALLBACK_URL'] == 'https://receiver.example.invalid/callback'
    assert settings['REPORT_PRODUCT_URL'] == 'https://catalog.example.invalid/products'


@pytest.mark.parametrize('first, second', list(combinations(COLLECTION_DEFAULTS, 2)))
def test_questionnaire_collections_are_pairwise_distinct(
    read_settings,
    first,
    second,
):
    with pytest.raises(EnvConfigError, match=f'SHULAN_{second}'):
        read_settings(**{first: 'fixture-shared-name', second: 'fixture-shared-name'})


@pytest.mark.parametrize('questionnaire_name', COLLECTION_DEFAULTS)
@pytest.mark.parametrize('existing_name', EXISTING_COLLECTIONS)
def test_questionnaire_collections_cannot_alias_any_existing_mongo_collection(
    read_settings,
    questionnaire_name,
    existing_name,
):
    with pytest.raises(EnvConfigError, match=f'SHULAN_{questionnaire_name}'):
        read_settings(**{questionnaire_name: 'fixture-collision', existing_name: 'fixture-collision'})


@pytest.mark.parametrize('questionnaire_name, default_name', COLLECTION_DEFAULTS.items())
def test_default_questionnaire_names_are_checked_against_custom_existing_names(
    read_settings,
    questionnaire_name,
    default_name,
):
    with pytest.raises(EnvConfigError, match=f'SHULAN_{questionnaire_name}'):
        read_settings(MONGODB_COLL_SESSIONS=default_name)


def test_independent_custom_collections_are_available_in_configuration_summary(read_settings):
    custom = {
        'MONGODB_COLL_QUESTIONNAIRES': 'fixture-questionnaires',
        'MONGODB_COLL_QUESTIONNAIRE_DOCUMENTS': 'fixture-questionnaire-documents',
        'MONGODB_COLL_QUESTIONNAIRE_CHUNKS': 'fixture-questionnaire-chunks',
        'MONGODB_COLL_QUESTIONNAIRE_FACTOR_TASKS': 'fixture-questionnaire-factor-tasks',
    }
    settings = read_settings(**custom)
    assert {name: settings[name] for name in custom} == custom
    assert all(f'SHULAN_{name}' in settings['summary']() for name in custom)


def test_collection_errors_are_reported_together_before_startup(read_settings):
    with pytest.raises(EnvConfigError) as caught:
        read_settings(
            MONGODB_COLL_QUESTIONNAIRE_DOCUMENTS='fixture-sessions',
            MONGODB_COLL_QUESTIONNAIRES='fixture-health-reports',
        )
    message = str(caught.value)
    assert 'SHULAN_MONGODB_COLL_QUESTIONNAIRE_DOCUMENTS' in message
    assert 'SHULAN_MONGODB_COLL_QUESTIONNAIRES' in message
