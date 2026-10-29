"""One place that turns HTTP outcomes into the two error kinds the agents understand (ports.py).

Timeouts, connection errors, 429 and 5xx are transient; every other 4xx is permanent.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..ports import PermanentError, TransientError


def request(
    client: httpx.Client, system: str, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    try:
        response = client.request(method, url, **kwargs)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise TransientError(system, f"{type(exc).__name__} on {method} {url}") from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientError(system, f"{response.status_code} on {method} {url}")
    if response.status_code >= 400:
        raise PermanentError(
            system, f"{response.status_code} on {method} {url}: {_brief(response)}"
        )
    return response


def _brief(response: httpx.Response) -> str:
    return response.text[:300].replace("\n", " ")
