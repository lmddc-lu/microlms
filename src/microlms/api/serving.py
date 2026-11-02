"""Streaming responses for stored content, with HTTP range support.

Media playback depends on ranges: the browser ``<video>`` element seeks by
sending ``Range: bytes=...`` requests, and mobile Safari refuses to play media
from servers that ignore them. Objects are streamed from the storage backend
in chunks, so a large file never has to fit in memory.

Only single ranges are honored; multipart ranges and malformed headers are
answered with the full 200 response, which HTTP permits.
"""

import mimetypes
import posixpath
from collections.abc import Iterator
from typing import BinaryIO

from fastapi import HTTPException, status
from fastapi.responses import Response, StreamingResponse

from microlms.storage.base import StorageBackend, StorageKeyNotFoundError

_CHUNK_SIZE = 1024 * 1024


class _RangeNotSatisfiableError(Exception):
    """Raised when a syntactically valid range lies outside the object."""


def _iter_window(handle: BinaryIO, length: int) -> Iterator[bytes]:
    """Yield ``length`` bytes from the window handle in chunks, then close it.

    Reads are bounded here, never past the window, so the handle only has to
    support chunked ``read`` (the ``open_range`` contract - no seeking).
    """
    try:
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Resolve a single-range ``bytes=`` header to an inclusive (start, end).

    Returns None for headers to ignore (other units, multipart or malformed
    ranges). Raises _RangeNotSatisfiableError when the range is valid but
    lies outside the object.
    """
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    start_text, separator, end_text = spec.strip().partition("-")
    if not separator:
        return None
    if start_text == "":
        # Suffix range: the last N bytes of the object
        if not end_text.isdigit():
            return None
        suffix = int(end_text)
        if suffix == 0 or size == 0:
            raise _RangeNotSatisfiableError
        return max(size - suffix, 0), size - 1
    if not start_text.isdigit() or (end_text != "" and not end_text.isdigit()):
        return None
    start = int(start_text)
    if end_text != "" and int(end_text) < start:
        return None
    if start >= size:
        raise _RangeNotSatisfiableError
    end = int(end_text) if end_text != "" else size - 1
    return start, min(end, size - 1)


def stream_storage_object(
    storage: StorageBackend, key: str, media_type: str, range_header: str | None
) -> Response:
    """Stream a stored object, answering single-range requests with 206.

    Pass the request's ``Range`` header value (or None). Unsatisfiable
    ranges get a 416 response. Raises StorageKeyNotFoundError when the key
    does not exist.
    """
    size = storage.size(key)
    try:
        requested = _parse_range(range_header, size) if range_header is not None else None
    except _RangeNotSatisfiableError:
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={"accept-ranges": "bytes", "content-range": f"bytes */{size}"},
        )
    if requested is None:
        start, length, response_status = 0, size, status.HTTP_200_OK
        headers = {
            "accept-ranges": "bytes",
            "content-length": str(size),
            "x-content-type-options": "nosniff",
        }
    else:
        start, end = requested
        length, response_status = end - start + 1, status.HTTP_206_PARTIAL_CONTENT
        headers = {
            "accept-ranges": "bytes",
            "x-content-type-options": "nosniff",
            "content-length": str(length),
            "content-range": f"bytes {start}-{end}/{size}",
        }
    return StreamingResponse(
        _iter_window(storage.open_range(key, start, length), length),
        status_code=response_status,
        media_type=media_type,
        headers=headers,
    )


def serve_package_file(
    storage: StorageBackend, storage_prefix: str, file_path: str, range_header: str | None
) -> Response:
    """Stream a file of an extracted content package stored under
    ``storage_prefix``, with its media type guessed from the file name.

    ``file_path`` comes from the request URL and is relative to the package
    root: a path climbing out of the package (``..``) or an absolute one is
    answered with 404, so one package can never reach another's files. A
    missing file is a 404 too.
    """
    relative_path = posixpath.normpath(file_path)
    if relative_path == ".." or relative_path.startswith(("/", "../")):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Package file path leaves the package: {file_path!r}",
        )
    media_type = mimetypes.guess_type(relative_path)[0] or "application/octet-stream"
    try:
        return stream_storage_object(
            storage, f"{storage_prefix}/{relative_path}", media_type, range_header
        )
    except StorageKeyNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
