"""Core settings, loaded from environment variables with the MICROLMS_ prefix.

Client projects subclass :class:`Settings` to add their own fields.
"""

from datetime import timedelta

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for a microLMS instance.

    Example: ``MICROLMS_DATABASE_URL=postgresql+psycopg://lms:lms@db/lms``.
    """

    model_config = SettingsConfigDict(
        env_prefix="MICROLMS_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )

    app_name: str = "microLMS"
    debug: bool = False

    # URL prefix the core routers and content modules mount under. Must be
    # empty or start with "/" and not end with "/" (FastAPI asserts this at
    # startup). The auth router always mounts at /auth, outside this prefix.
    api_prefix: str = "/api/v1"

    # SQLAlchemy database URL; the client project chooses the driver
    database_url: str

    # xAPI Learning Record Store endpoint (e.g. lrsql), unset if xAPI is not used
    lrs_url: str | None = None

    # Basic-auth credentials for the LRS xAPI endpoint; required by the cmi5
    # module to write launch data and forward statements on the learner's behalf
    lrs_api_key: str | None = None
    lrs_api_secret: str | None = None

    # Stable homePage IRI stamped on the canonical xAPI actor account. When
    # unset, each request's base URL is used instead, which fragments a user's
    # identity as soon as the API is reachable through more than one hostname
    # (e.g. a frontend proxy and a direct origin). Set it once per deployment
    # and never change it after real statements exist.
    lrs_actor_home_page: str | None = None
    # How long a content launch credential (the SCORM and H5P player token,
    # the cmi5 session) stays valid after the launch, at most: it also stops
    # once the content reports the session ended (SCORM Terminate, cmi5
    # terminated). From the env as seconds or an ISO 8601 duration, e.g.
    # MICROLMS_LAUNCH_MAX_AGE=P1D.
    launch_max_age: timedelta = timedelta(days=1)

    # xAPI ingestion (the `xapi` module): service keys a trusted external source
    # presents in the `X-Ingest-Key` header to push statements.
    # Empty = ingestion refused. From the env as a JSON array,
    # e.g. MICROLMS_XAPI_INGEST_KEYS='["key-a","key-b"]'.
    xapi_ingest_keys: frozenset[str] = frozenset()

    # Slug of the organization self-registered users join (the bundled auth
    # backends' /auth/register). None = self-registration is refused, so
    # accounts are only created by management. The org must exist; every org
    # role is held inside an organization, so there is no org-less
    # alternative.
    registration_org_slug: str | None = None

    # Guest (unauthenticated) xAPI ingestion: the org role given to a guest
    # identity provisioned on first submission, and the slug of the
    # organization it is held in. Set both or neither; None = guest ingestion
    # is disabled. The role must be an org role of the access policy, and the
    # org should be one no org manager manages, since its managers would see
    # every guest.
    xapi_guest_role: str | None = None
    xapi_guest_org_slug: str | None = None

    # LTI 1.3 platform (the `lti` module): the issuer identifier tools
    # register (an https URL with no query or fragment, stable for the life of
    # the deployment) and the PEM RSA private key signing launches and access
    # tokens. Its public half is served at /lti/jwks under a kid derived from
    # the key, so replacing the key rotates the kid too.
    lti_issuer: str | None = None
    lti_private_key: str | None = None
