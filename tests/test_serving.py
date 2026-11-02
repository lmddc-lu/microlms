"""Streaming stored objects over HTTP."""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def test_streaming_uses_ranged_windows_without_seeking() -> None:
    """Content streaming must fit object-store backends: reads arrive as one
    (start, length) window per request and the returned handle only needs
    chunked read/close - never seek (an S3 ranged GET body is not seekable)."""
    import io

    from fastapi.responses import Response

    from microlms.api.serving import stream_storage_object
    from microlms.storage.base import StorageKeyNotFoundError

    class WindowReader:
        """A read/close-only handle, like a ranged GET body."""

        def __init__(self, data: bytes) -> None:
            self._buffer = io.BytesIO(data)

        def read(self, size: int) -> bytes:
            return self._buffer.read(size)

        def close(self) -> None:
            self._buffer.close()

    class RangedMemoryStorage:
        """In-memory stand-in for an object store, recording requested windows."""

        def __init__(self, objects: dict[str, bytes]) -> None:
            self._objects = objects
            self.windows: list[tuple[int, int]] = []

        def _data(self, key: str) -> bytes:
            if key not in self._objects:
                raise StorageKeyNotFoundError(f"No stored object for key {key!r}")
            return self._objects[key]

        def open_range(self, key: str, start: int, length: int) -> WindowReader:
            data = self._data(key)
            self.windows.append((start, length))
            return WindowReader(data[start : start + length])

        def size(self, key: str) -> int:
            return len(self._data(key))

    storage = RangedMemoryStorage({"pkg/media.bin": b"0123456789"})
    app = FastAPI()

    @app.get("/blob")
    def blob(request: Request) -> Response:
        return stream_storage_object(
            storage, "pkg/media.bin", "application/octet-stream", request.headers.get("range")
        )

    with TestClient(app) as client:
        full = client.get("/blob")
        assert full.status_code == 200
        assert full.content == b"0123456789"

        part = client.get("/blob", headers={"Range": "bytes=2-5"})
        assert part.status_code == 206
        assert part.content == b"2345"
        assert part.headers["content-range"] == "bytes 2-5/10"

        suffix = client.get("/blob", headers={"Range": "bytes=-3"})
        assert suffix.status_code == 206
        assert suffix.content == b"789"

        assert client.get("/blob", headers={"Range": "bytes=10-"}).status_code == 416

    # Exactly one window per served response; the 416 never opened one
    assert storage.windows == [(0, 10), (2, 4), (7, 3)]
