"""Offline report follow-up acceptance; real dependencies use the API entrypoint."""

import asyncio
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.unit._support.harnesses.followup_options import parse_args


def main():
    args = parse_args(online=False)
    from tests.unit._support.harnesses.run_followups import async_main

    try:
        sys.exit(asyncio.run(async_main(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == '__main__':
    main()
