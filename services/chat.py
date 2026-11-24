"""Deprecated module alias for :mod:`modules.chat.application.service`."""

import sys

from modules.chat.application import service as _implementation

sys.modules[__name__] = _implementation
