"""Explicit archived-release fixtures, never a production archive fallback."""
import pytest


@pytest.fixture
def historical_ordered_release(monkeypatch):
    from adapters.gut_barrier import frontend_template
    from modules.gut_barrier.display_versions import ORDERED_REFERENCE_DISPLAY_TEMPLATE
    from tests.unit.gut_barrier.preview.archive_support import historical_archive

    directory = historical_archive()
    path = directory / f"{ORDERED_REFERENCE_DISPLAY_TEMPLATE}.json"
    if not path.is_file():
        pytest.fail(f"configured historical template archive is incomplete: {path}")
    original_path = frontend_template._archived_template_path

    def archived_path(version, archive_directory):
        if version == ORDERED_REFERENCE_DISPLAY_TEMPLATE and archive_directory is None:
            archive_directory = directory
        return original_path(version, archive_directory)

    monkeypatch.setattr(frontend_template, "_archived_template_path", archived_path)
    return directory
