import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.artifact_paths import PROJECT_ROOT, artifact_output
from scripts.generate_gut_barrier_report import generate
from scripts.render_gut_barrier_callback import render_callback


def test_default_artifact_destination_is_external_and_unique(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    first, second = artifact_output(), artifact_output()
    assert first.parent == second.parent == tmp_path / "Documents" / "ShulanArtifacts"
    assert first != second
    assert not first.parent.exists()


def test_explicit_external_destination(tmp_path):
    assert artifact_output(tmp_path / "explicit") == tmp_path / "explicit"


def test_default_and_explicit_artifact_destinations_do_not_load_configuration(monkeypatch, tmp_path):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "config" or name.startswith("config."):
            raise AssertionError("preview artifact paths must not load configuration")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert artifact_output().parent == tmp_path / "Documents" / "ShulanArtifacts"
    assert artifact_output(tmp_path / "explicit") == tmp_path / "explicit"
    with pytest.raises(ValueError, match="outside the project"):
        artifact_output(PROJECT_ROOT / "outputs")


@pytest.mark.parametrize("suffix", ["", "outputs/test-preview", "tmp/../report"])
def test_repository_destinations_are_rejected(suffix):
    with pytest.raises(ValueError, match="outside the project"):
        artifact_output(PROJECT_ROOT / suffix)


def test_relative_repository_destination_is_rejected(monkeypatch):
    monkeypatch.chdir(PROJECT_ROOT)
    with pytest.raises(ValueError, match="outside the project"):
        artifact_output("outputs/preview")


def test_symlink_repository_destination_is_rejected(tmp_path, symlink_or_skip):
    alias = tmp_path / "checkout"
    symlink_or_skip(alias, PROJECT_ROOT, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the project"):
        artifact_output(alias / "outputs/preview")


def test_default_home_cannot_bypass_repository_guard(monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: PROJECT_ROOT)
    with pytest.raises(ValueError, match="outside the project"):
        artifact_output()


@pytest.mark.asyncio
async def test_every_cli_entry_rejects_repository_write_before_reading_input():
    destination = PROJECT_ROOT / "outputs/forbidden-artifact-test"
    with pytest.raises(ValueError, match="outside the project"):
        await generate(SimpleNamespace(output=destination, input="not-read.json"))
    with pytest.raises(ValueError, match="outside the project"):
        render_callback({}, destination)
    assert not destination.exists()
