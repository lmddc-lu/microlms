"""Local filesystem storage, the default backend for development."""

import shutil
from pathlib import Path
from typing import BinaryIO

from microlms.storage.base import StorageKeyNotFoundError


class FilesystemStorage:
    """Stores objects as files under a root directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError(f"Storage key escapes the storage root: {key!r}")
        return path

    def _existing_path(self, key: str) -> Path:
        path = self._path(key)
        if not path.is_file():
            raise StorageKeyNotFoundError(f"No stored object for key {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def open_range(self, key: str, start: int, length: int) -> BinaryIO:
        handle = self._existing_path(key).open("rb")
        handle.seek(start)
        return handle

    def size(self, key: str) -> int:
        return self._existing_path(key).stat().st_size

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._existing_path(key).unlink()

    def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()
