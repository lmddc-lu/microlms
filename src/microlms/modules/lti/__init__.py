"""LTI module: the platform side of LTI 1.3 with Assignment and Grade Services.

Places resources of external tools in courses and launches learners into
them. Administrators register a tool (the out-of-band security contract);
authors place a tool resource as one language variant of a course activity
(a resource link with one declarative line item); a learner's launch runs
the OIDC third-party initiated login and posts a signed ``id_token`` to the
tool. The tool reports back through AGS - it obtains an access token with a
signed client assertion and publishes scores, and a ``Completed`` score
satisfies the variant, driving enrollment completion like the other content
modules. The launch and the scores are recorded as xAPI statements in the LRS
(see :mod:`microlms.modules.lti.statements`).
"""

from microlms.modules.lti.links import purge_variant
from microlms.modules.lti.router import build_router

__all__ = ["build_router", "purge_variant"]
