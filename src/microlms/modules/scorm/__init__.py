"""SCORM module: package import, player, runtime and SCORM-to-xAPI conversion.

Supports SCORM 1.2 and SCORM 2004 packages: import parses ``imsmanifest.xml``
and detects the edition, launch serves a player page exposing the matching
runtime object to the SCO (``API`` for 1.2, ``API_1484_11`` for 2004), and
committed runtime data is converted to xAPI statements and drives enrollment
completion.
"""

from microlms.modules.scorm.importer import purge_variant
from microlms.modules.scorm.router import build_router

__all__ = ["build_router", "purge_variant"]
