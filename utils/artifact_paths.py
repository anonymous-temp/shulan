"""Resolve local diagnostic artifacts without allowing writes into source code."""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def external_artifact_path(output: str | Path) -> Path:
    """Validate without creating files; resolve relative paths and symlinks."""
    resolved = Path(output).expanduser().resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise ValueError('artifact output must be outside the project directory')
    return resolved
