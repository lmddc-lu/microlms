"""H5P module: package import, standalone player and xAPI tracking.

Plays ``.h5p`` packages authored externally (e.g. with Lumi) and exported with
their libraries bundled: import stores the extracted package as one language
variant of a course activity, launch serves a player page running the vendored
h5p-standalone player, and the xAPI statements the content emits are forwarded
to the LRS server-side and drive enrollment completion. There is no in-browser
authoring - content is created outside the LMS and uploaded, like the scorm
and cmi5 modules' packages.
"""

from microlms.modules.h5p.importer import purge_variant
from microlms.modules.h5p.router import build_router

__all__ = ["build_router", "purge_variant"]
