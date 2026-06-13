"""HTTP layer: endpoints, transports and the escalation ladder."""

from .base import SimpleResponse, check_graphql_errors, classify_status, parse_json
from .endpoints import DOC_ID_TIMELINE, GRAPHQL_QUERY_URL, WEB_PROFILE_INFO_URL, default_headers
from .http_transport import AnonymousTransport, AuthenticatedTransport, HttpxTransport
from .impersonate_transport import ImpersonatedTransport
from .provider import EscalatingTransportProvider, default_transport_factory

__all__ = [
    "DOC_ID_TIMELINE",
    "GRAPHQL_QUERY_URL",
    "WEB_PROFILE_INFO_URL",
    "AnonymousTransport",
    "AuthenticatedTransport",
    "EscalatingTransportProvider",
    "HttpxTransport",
    "ImpersonatedTransport",
    "SimpleResponse",
    "check_graphql_errors",
    "classify_status",
    "default_headers",
    "default_transport_factory",
    "parse_json",
]
