"""Optional modules a client project enables via ``create_app``.

Each module is self-contained and exposes a ``build_router()`` factory. The
factory is imported lazily, only when the client selects the module, so a module
that pulls an optional extra (e.g. cmi5 needs httpx) does not force that
dependency on clients that never enable it. ``scorm``, ``cmi5`` and ``h5p`` play
content; ``lti`` launches content hosted by external LTI 1.3 tools; ``xapi``
ingests statements from an external activity source.
"""

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter
from sqlalchemy.orm import Session

from microlms.storage.base import StorageBackend

ModuleName = Literal["scorm", "cmi5", "h5p", "lti", "xapi"]

MODULE_NAMES: frozenset[ModuleName] = frozenset(("scorm", "cmi5", "h5p", "lti", "xapi"))

# Tears down all content a module owns for one activity variant (its rows and
# stored package files), within the caller's transaction. See create_app,
# which dispatches per variant on its owning module.
VariantPurger = Callable[[Session, StorageBackend, int], None]


class UnknownModuleError(Exception):
    """Raised when create_app receives a module name that does not exist."""


class ContentModuleNotEnabledError(Exception):
    """Raised when content is owned by a module the app did not enable, so it
    cannot be torn down (e.g. deleting a scorm variant with scorm disabled)."""


def _noop_purger(session: Session, storage: StorageBackend, variant_id: int) -> None:
    """Purger for modules that own no course content (nothing to tear down)."""
    return None


def build_module_router(name: ModuleName) -> APIRouter:
    if name == "scorm":
        from microlms.modules.scorm import build_router
    elif name == "cmi5":
        from microlms.modules.cmi5 import build_router
    elif name == "h5p":
        from microlms.modules.h5p import build_router
    elif name == "lti":
        from microlms.modules.lti import build_router
    elif name == "xapi":
        from microlms.modules.xapi import build_router
    else:
        raise UnknownModuleError(f"Unknown module {name!r}, available: {sorted(MODULE_NAMES)}")
    return build_router()


def build_variant_purger(name: ModuleName) -> VariantPurger:
    """The teardown function for a content module's variant data, imported
    lazily like ``build_module_router``. Modules that own no course content
    (``xapi`` ingestion) get a no-op."""
    if name == "scorm":
        from microlms.modules.scorm import purge_variant
    elif name == "cmi5":
        from microlms.modules.cmi5 import purge_variant
    elif name == "h5p":
        from microlms.modules.h5p import purge_variant
    elif name == "lti":
        from microlms.modules.lti import purge_variant
    elif name == "xapi":
        return _noop_purger
    else:
        raise UnknownModuleError(f"Unknown module {name!r}, available: {sorted(MODULE_NAMES)}")
    return purge_variant
