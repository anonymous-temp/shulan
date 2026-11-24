"""Deprecated module alias for :mod:`modules.analysis.application.service`."""

import sys

from modules.analysis.application import service as _implementation

sys.modules[__name__] = _implementation
