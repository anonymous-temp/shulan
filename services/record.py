"""Deprecated module alias for :mod:`modules.record.application.service`."""

import sys

from modules.record.application import service as _implementation

sys.modules[__name__] = _implementation
