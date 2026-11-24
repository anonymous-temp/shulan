"""Create the Tornado application from an already composed container."""

from __future__ import annotations

import tornado.web

from bootstrap.container import AppContainer
from config.env import DEBUG
from web.handlers import log_tornado_request
from web.routes import build_routes


def create_web_application(
        container: AppContainer,
        *,
        debug: bool = DEBUG,
) -> tornado.web.Application:
    """Build Tornado without constructing business or infrastructure objects."""
    settings = container.compatibility_settings()
    settings.update({
        'app_container': container,
        'log_function': log_tornado_request,
    })
    return tornado.web.Application(
        build_routes(container),
        debug=debug,
        **settings,
    )


__all__ = ['create_web_application']
