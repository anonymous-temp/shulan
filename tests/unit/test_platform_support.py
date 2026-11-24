"""Guard test portability without weakening application or security assertions."""

from pathlib import Path
from unittest.mock import Mock

import pytest


def test_collected_test_ids_fit_windows_environment_limit(request):
    # pytest publishes the node ID and stage as PYTEST_CURRENT_TEST.
    oversized = [
        item.nodeid.split('[')[0]
        for item in request.session.items
        if len((item.nodeid + ' (teardown)').encode('utf-16-le')) // 2 >= 32767
    ]
    assert oversized == [], f'Use short parameter IDs for: {oversized}'


@pytest.mark.parametrize('directory', [False, True])
def test_symlink_helper_preserves_link_creation_arguments(symlink_or_skip, directory):
    link = Mock(spec=Path)
    target = Path('target')
    assert symlink_or_skip(link, target, target_is_directory=directory) is link
    link.symlink_to.assert_called_once_with(target, target_is_directory=directory)


def test_symlink_helper_skips_missing_windows_privilege(symlink_or_skip):
    error = OSError('symbolic link privilege unavailable')
    error.winerror = 1314
    link = Mock(spec=Path)
    link.symlink_to.side_effect = error
    with pytest.raises(pytest.skip.Exception, match='WinError 1314'):
        symlink_or_skip(link, Path('target'))


@pytest.mark.parametrize('error_type', [FileExistsError, PermissionError, OSError])
def test_symlink_helper_does_not_hide_other_failures(symlink_or_skip, error_type):
    error = error_type('unexpected filesystem failure')
    link = Mock(spec=Path)
    link.symlink_to.side_effect = error
    with pytest.raises(error_type) as raised:
        symlink_or_skip(link, Path('target'))
    assert raised.value is error
