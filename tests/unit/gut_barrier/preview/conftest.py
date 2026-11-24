"""Latest preview tests run standalone; archived releases are opt-in."""
import pytest

from tests.unit.gut_barrier.preview import archive_support


@pytest.fixture
def historical_templates(monkeypatch):
    from adapters.gut_barrier import frontend_template

    directory = archive_support.historical_archive()
    original = frontend_template._archived_template_path

    def explicit_test_archive(version, archive_directory):
        return original(version, directory if archive_directory is None else archive_directory)

    monkeypatch.setattr(frontend_template, "_archived_template_path", explicit_test_archive)
    return directory
