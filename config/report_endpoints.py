"""Validate deployment-owned report endpoints before configuration or HTTP I/O."""

from __future__ import annotations

from ipaddress import ip_address
from socket import inet_aton
from urllib.parse import urlsplit


def default_evaluate_callback_url(shared_url: str) -> str:
    """Use the deployment's own origin for the versioned assessment callback."""
    endpoint = urlsplit(shared_url)
    if endpoint.path.removesuffix('/') == '/api/basic-health-assessment/ai-full-back/callback':
        return endpoint._replace(path='/api/basic-health-assessment/ai-full-back-2026-11/callback').geturl()
    return shared_url


def validate_report_endpoint(
    value: str,
    *,
    setting: str = 'REPORT_CALLBACK_URL',
    environment: str | None = None,
    allow_empty: bool = False,
) -> str:
    """Keep endpoint values configurable while blocking known test targets in production.

    Runtime calls also validate stored callback targets, rather than assuming
    an outbox created under an older deployment has the current configuration.
    Error messages deliberately omit URLs, which can contain signed queries.
    """
    error = f'{setting} must be an HTTP(S) URL without credentials, fragment, whitespace or backslashes'
    if not isinstance(value, str):
        raise ValueError(error)
    value = value.strip()
    if not value and allow_empty:
        return ''
    if not value or any(character.isspace() or ord(character) < 32 for character in value) or '\\' in value:
        raise ValueError(error)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme not in {'http', 'https'}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port == 0
        ):
            raise ValueError(error)
        host = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if not host or '%' in host:
            raise ValueError(error)
    except (ValueError, UnicodeError):
        raise ValueError(error) from None
    if environment is None:
        from config.env import ENVIRONMENT

        environment = ENVIRONMENT
    if environment == 'production':
        try:
            address = ip_address(host)
        except ValueError:
            # HTTP resolvers also accept abbreviated/decimal IPv4 spellings.
            # inet_aton parses locally and does not perform a DNS lookup.
            try:
                loopback = ip_address(inet_aton(host)).is_loopback
            except OSError:
                loopback = host == 'localhost' or host.endswith('.localhost')
        else:
            loopback = address.is_loopback or (
                getattr(address, 'ipv4_mapped', None) is not None and address.ipv4_mapped.is_loopback
            )
        if parsed.scheme != 'https' or host == 'test.slmc.top' or host.endswith('.test.slmc.top') or loopback:
            raise ValueError(f'{setting} must use a production HTTPS endpoint, not a test or loopback endpoint')
    return value
