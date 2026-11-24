"""Only production product overrides belong in gut-barrier environment settings."""

import ast
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[3]
REMOVED_NAMES = (
    "GUT_BARRIER_ENABLED", "GUT_BARRIER_CALLBACK_URL", "GUT_BARRIER_TEMPLATE_ARCHIVE_DIR", "ARTIFACT_DIR",
)
PRODUCT_NAME = "GUT_BARRIER_PRODUCT_URL"


@pytest.fixture
def read_settings(monkeypatch):
    for name in (*REMOVED_NAMES, PRODUCT_NAME):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"SHULAN_{name}", raising=False)

    def read(**values):
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        with patch("loongspears.utils.env.load_env", return_value=None):
            return runpy.run_path(str(ROOT / "config/env.py"))

    return read


def test_product_default_inherits_existing_report_setting(read_settings):
    settings = read_settings()
    assert settings[PRODUCT_NAME] == settings["REPORT_PRODUCT_URL"]
    assert all(name not in settings for name in REMOVED_NAMES)


@pytest.mark.parametrize("prefix", ["", "SHULAN_"])
@pytest.mark.parametrize("raw", ["", " https://unit-test.invalid/gut-products "])
def test_product_override_is_loaded_centrally(read_settings, prefix, raw):
    settings = read_settings(**{f"{prefix}{PRODUCT_NAME}": raw})
    assert settings[PRODUCT_NAME] == raw.strip()


@pytest.mark.parametrize("value", ["", "https://unit-test.invalid/new-products"])
def test_canonical_product_value_wins_over_legacy_even_when_empty(read_settings, value):
    settings = read_settings(GUT_BARRIER_PRODUCT_URL="https://unit-test.invalid/old-products",
                             SHULAN_GUT_BARRIER_PRODUCT_URL=value)
    assert settings[PRODUCT_NAME] == value


def test_removed_settings_are_neither_loaded_nor_shown_in_summary(read_settings):
    values = {f"{prefix}{name}": "removed-setting-must-be-ignored"
              for prefix in ("", "SHULAN_") for name in REMOVED_NAMES}
    settings = read_settings(**values)
    assert all(name not in settings for name in REMOVED_NAMES)
    assert all(name not in settings["summary"]() for name in REMOVED_NAMES)


@pytest.mark.parametrize("filename", [
    ".env.example", ".env.example.dev", ".env.example.test", ".env.example.production",
])
def test_examples_preserve_shared_product_default_without_removed_settings(filename):
    path = ROOT / filename
    values = dotenv_values(path, interpolate=False)
    text = path.read_text(encoding="utf-8")
    assert "SHULAN_GUT_BARRIER_PRODUCT_URL" not in values
    assert "SHULAN_REPORT_PRODUCT_URL" in values
    assert all(name not in text for name in REMOVED_NAMES)


@pytest.mark.parametrize("filename", [
    "adapters/gut_barrier/factory.py", "adapters/gut_barrier/frontend_template.py", "scripts/artifact_paths.py",
])
def test_consumers_do_not_read_process_environment_directly(filename):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.Attribute) and node.attr in {"getenv", "environ"}
                   for node in ast.walk(tree))
    assert not any(isinstance(node, ast.ImportFrom) and node.module == "os"
                   and any(alias.name in {"getenv", "environ"} for alias in node.names)
                   for node in ast.walk(tree))
