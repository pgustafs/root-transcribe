"""Shared byte-range streaming for authenticated recording and clip routes.

Authorization and short-lived playback tokens are checked by the routes before
calling this proxy; the object store need not be browser-reachable.
"""

from __future__ import annotations

from collections.abc import Callable

import anyio
from fastapi import Request, Response, status
from fastapi.responses import StreamingResponse

from app.services import http_range


async def range_proxy_response(
    request: Request,
    *,
    store,
    key: str,
    size: int,
    etag: str,
    media_type: str,
    base_headers: dict[str, str],
    observe: Callable[[int], None] | None = None,
) -> Response:
    """The shared 200/206/416 Range-proxy tail.

    ``If-Range`` with a stale ETag falls back to a full 200 (the object was
    replaced mid-session); HEAD answers metadata without a body; ``observe``
    (optional) receives the response status for caller-owned metrics.
    """

    def _observe(status_code: int) -> None:
        if observe is not None:
            observe(status_code)

    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if if_range is not None and if_range.strip('"') != etag:
        range_header = None  # the object changed since the client cached it

    try:
        byte_range = http_range.parse_range(range_header, size)
    except http_range.UnsatisfiableRange as exc:
        _observe(416)
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={**base_headers, "Content-Range": str(exc)},
        )

    if request.method == "HEAD":
        headers = {**base_headers, "Content-Length": str(size)}
        return Response(status_code=200, headers=headers, media_type=media_type)

    if byte_range is None:
        chunks = await anyio.to_thread.run_sync(store.stream_sync, key)
        _observe(200)
        return StreamingResponse(
            chunks,
            media_type=media_type,
            headers={**base_headers, "Content-Length": str(size)},
        )
    chunks = await anyio.to_thread.run_sync(
        store.get_range, key, byte_range.start, byte_range.end
    )
    _observe(206)
    return StreamingResponse(
        chunks,
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media_type,
        headers={
            **base_headers,
            "Content-Range": byte_range.content_range(size),
            "Content-Length": str(byte_range.length),
        },
    )
