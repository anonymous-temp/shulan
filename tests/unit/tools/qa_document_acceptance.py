"""Offline qa document acceptance entrypoint."""

import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.unit._support.harnesses.qa_document_acceptance import main

if __name__ == '__main__':
    raise SystemExit(main())
