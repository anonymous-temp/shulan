"""Explicit opt-in for regression fixtures archived outside the checkout."""
from pathlib import Path

import pytest


# Set temporarily in the test process for a manual historical regression.
# Keep machine-specific paths and archived assets outside the source checkout.
HISTORICAL_ARCHIVE_DIR: Path | None = None


def historical_archive() -> Path:
    configured = HISTORICAL_ARCHIVE_DIR
    if not configured:
        pytest.skip("historical template regression requires an explicit HISTORICAL_ARCHIVE_DIR test path")
    directory = Path(configured)
    if not directory.is_absolute() or not directory.is_dir():
        pytest.fail(f"invalid historical template archive: {directory}")
    return directory


def historical_asset(version: str) -> Path:
    path = historical_archive() / f"gut-barrier-static.{version}.json"
    if not path.is_file():
        pytest.fail(f"configured historical template archive is incomplete: {path}")
    return path
