"""Core settings, loaded from environment variables with the MICROLMS_ prefix.

Client projects subclass :class:`Settings` to add their own fields.
"""

from datetime import timedelta

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for a microLMS instance.

    Each field is read from the environment variable of the same name in upper
    case with the ``MICROLMS_`` prefix, or from a ``.env`` file. Example:
    ``MICROLMS_DATABASE_URL=postgresql+psycopg://lms:lms@db/lms``.
    """

    # use_attribute_docstrings turns the docstring under each field into its
    # description, so the field documentation lives in one place
    model_config = SettingsConfigDict(
        env_prefix="MICROLMS_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        use_attribute_docstrings=True,
    )

    app_name: str = "microLMS"
    """Application title, shown in the OpenAPI docs and the /health response."""

    debug: bool = False
    """FastAPI debug mode: tracebacks in error responses. Never in production."""

    api_prefix: str = "/api/v1"
    """URL prefix the core routers and content modules mount under. Must be
    empty or start with "/" and not end with "/" (FastAPI asserts this at
    startup). The auth router always mounts at /auth, outside this prefix."""

    database_url: str
    """SQLAlchemy database URL; the client project chooses the driver."""

    lrs_url: str | None = None
    """xAPI Learning Record Store endpoint (e.g. lrsql), unset if xAPI is not
    used. Required by the content modules."""

    lrs_api_key: str | None = None
    """Basic-auth user for the LRS xAPI endpoint. Required by the content
    modules, which write launch data and forward statements on the learner's
    behalf."""

    lrs_api_secret: str | None = None
    """Basic-auth password for the LRS xAPI endpoint, paired with
    ``lrs_api_key``."""

    lrs_actor_home_page: str | None = None
    """Stable homePage IRI stamped on the canonical xAPI actor account. When
    unset, each request's base URL is used instead, which fragments a user's
    identity as soon as the API is reachable through more than one hostname
    (e.g. a frontend proxy and a direct origin). Set it once per deployment
    and never change it after real statements exist."""

    launch_max_age: timedelta = timedelta(days=1)
    """How long a content launch credential (the SCORM and H5P player token,
    the cmi5 session) stays valid after the launch, at most: it also stops
    once the content reports the session ended (SCORM Terminate, cmi5
    terminated). From the env as seconds or an ISO 8601 duration, e.g.
    ``MICROLMS_LAUNCH_MAX_AGE=P1D``."""

    xapi_ingest_keys: frozenset[str] = frozenset()
    """Service keys a trusted external source presents in the
    ``X-Ingest-Key`` header to push statements through the ``xapi`` module.
    Empty = ingestion refused. From the env as a JSON array, e.g.
    ``MICROLMS_XAPI_INGEST_KEYS='["key-a","key-b"]'``."""

    registration_org_slug: str | None = None
    """Slug of the organization self-registered users join (the bundled auth
    backends' /auth/register). None = self-registration is refused, so
    accounts are only created by management. The org must exist; every org
    role is held inside an organization, so there is no org-less
    alternative."""

    xapi_guest_role: str | None = None
    """Org role given to the guest identity provisioned on a guest's first
    unauthenticated xAPI submission. Set together with
    ``xapi_guest_org_slug``; None = guest ingestion is disabled. The role
    must be an org role of the access policy."""

    xapi_guest_org_slug: str | None = None
    """Slug of the organization guest identities are held in. Set together
    with ``xapi_guest_role``. Pick an org no org manager manages, since its
    managers would see every guest."""

    lti_issuer: str | None = None
    """Issuer identifier tools register with the LTI 1.3 platform (the
    ``lti`` module): an https URL with no query or fragment, stable for the
    life of the deployment."""

    lti_private_key: str | None = None
    """PEM RSA private key signing LTI launches and access tokens. Its public
    half is served at /lti/jwks under a kid derived from the key, so
    replacing the key rotates the kid too."""
