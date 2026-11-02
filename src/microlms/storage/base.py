"""Storage contract for course packages and uploaded content.

Client projects pick a bundled backend or implement their own (e.g. S3,
Azure Blob) against this protocol. Reads are window-based (``open_range``)
so object stores can serve them with a single ranged GET and never need a
seekable handle.
"""

from typing import BinaryIO, Protocol


class StorageKeyNotFoundError(Exception):
    """Raised when a storage key does not exist."""


class StorageBackend(Protocol):
    """Storage extension point passed to ``create_app``."""

    def put(self, key: str, data: bytes) -> None:
        """Store data under the given key, overwriting any existing value."""
        ...

    def open_range(self, key: str, start: int, length: int) -> BinaryIO:
        """Return a readable handle positioned at byte ``start`` of the object.

        The handle only needs to support chunked ``read`` and ``close`` (no
        seeking), so an object store can back it with one ranged GET. It must
        deliver the ``length`` bytes of the window without loading the whole
        object in memory; the caller never reads further (a filesystem handle
        may continue into the rest of the file) and closes the handle.
        Raises StorageKeyNotFoundError when the key does not exist.
        """
        ...

    def size(self, key: str) -> int:
        """Return the size in bytes of the stored object.

        Raises StorageKeyNotFoundError when the key does not exist.
        """
        ...

    def exists(self, key: str) -> bool:
        """Return whether the key exists."""
        ...

    def delete(self, key: str) -> None:
        """Delete the key.

        Raises StorageKeyNotFoundError when the key does not exist.
        """
        ...

    def delete_prefix(self, prefix: str) -> None:
        """Delete every key under ``prefix`` (its whole subtree).

        Idempotent: deleting a prefix with no keys is a no-op, never an error.
        Content modules use it to remove all files of a package at once (they
        store each package under a single prefix of its own). The prefix is a
        path: ``scorm/12`` covers ``scorm/12/...`` but never ``scorm/12-ab``.
        """
        ...
