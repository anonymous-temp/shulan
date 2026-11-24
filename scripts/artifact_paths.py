"""Keep local patient reports and preview artifacts outside the source checkout."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def artifact_output(output: str | Path | None = None, *, prefix: str = "gut-barrier") -> Path:
    """Resolve a fresh default location without creating files or directories.

    Explicit destinations remain supported, but neither a relative path nor a
    symlink may put medical artifacts back in the application checkout.
    """
    if output is None:
        root = Path.home() / "Documents" / "ShulanArtifacts"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = root / f"{prefix}-{stamp}-{uuid4().hex[:8]}"
    resolved = Path(output).expanduser().resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise ValueError("report artifacts must be stored outside the project directory; use --output")
    return resolved
