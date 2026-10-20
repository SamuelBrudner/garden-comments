"""The thin urllib transport behind :func:`crop_explorer.youtube.fetch_comments`.

Standard library only: ``urllib.request`` honours ``HTTPS_PROXY`` and verifies certificates.
Each call is one GET with the caller's headers (the API key header) and the configured
timeout. Redirects are refused, because urllib would resend every header, the API key
included, to the new address. API error responses become :func:`crop_explorer.youtube.api_error`
messages; network failures become one :class:`~crop_explorer.youtube.YouTubeError` line.
Neither ever carries request headers or the response body's own text.
"""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from collections.abc import Mapping

from crop_explorer.youtube import FetchJson, YouTubeError, api_error, decode_json

__all__ = ["urllib_transport"]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an error instead of following it with the API key header."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Refuse the redirect (urllib then raises ``HTTPError`` with the 3xx status)."""


def urllib_transport(timeout_s: float, *handlers: urllib.request.BaseHandler) -> FetchJson:
    """Return a ``fetch_json(url, headers)`` that GETs one API page with urllib.

    Parameters
    ----------
    timeout_s
        Seconds each request may take (``youtube.timeout_s``).
    *handlers
        Extra urllib handlers; tests pass an in-process ``HTTPSHandler`` so no socket opens.

    Returns
    -------
    FetchJson
        Fetches ``url`` with ``headers`` and returns the decoded JSON object; raises
        :class:`~crop_explorer.youtube.YouTubeError` for an error status, a redirect, a body
        that is not a JSON object, or a failed connection.
    """
    opener = urllib.request.build_opener(_RefuseRedirects(), *handlers)

    def fetch_json(url: str, headers: Mapping[str, str]) -> dict[str, object]:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        return decode_json(_read(opener, request, timeout_s))

    return fetch_json


def _read(
    opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout_s: float
) -> bytes:
    try:
        with opener.open(request, timeout=timeout_s) as response:
            body: bytes = response.read()
    except urllib.error.HTTPError as exc:  # before OSError: HTTPError is one
        raise api_error(exc.code, _error_body(exc)) from None
    except (OSError, http.client.HTTPException) as exc:
        raise YouTubeError(f"cannot reach the YouTube Data API: {_reason(exc)}") from None
    return body


def _error_body(exc: urllib.error.HTTPError) -> bytes:
    """Read an error response's body; an unreadable one counts as empty."""
    try:
        body: bytes = exc.read()
    except (OSError, http.client.HTTPException):
        body = b""
    finally:
        exc.close()
    return body


def _reason(exc: OSError | http.client.HTTPException) -> str:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return str(reason) or type(exc).__name__
