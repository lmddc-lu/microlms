"""LTI ORM models: registered tools, resource links, launches and results.

Core tables (``lms_`` prefix). The platform side of LTI 1.3: a registered
tool holds the security contract (client and deployment ids, the tool's OIDC
and key set URLs); a resource link places one tool resource in a course as
one language variant of an activity, with a single declarative line item;
a launch carries a learner's in-flight OIDC login between the launch
endpoint and the authorization endpoint; a result keeps the latest score the
tool published for one enrollment.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from microlms.db.base import Base


class LtiTool(Base):
    """A tool registered with the platform, deployed once (single tenant)."""

    __tablename__ = "lms_lti_tool"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # OAuth2 client id the platform generated at registration
    client_id: Mapped[str] = mapped_column(String(255), unique=True)
    # Immutable deployment id the platform generated at registration
    deployment_id: Mapped[str] = mapped_column(String(255))
    # The tool's third-party initiated login endpoint
    oidc_login_url: Mapped[str] = mapped_column(String(2000))
    # Redirect URIs the tool registered; the id_token is only ever posted to one
    redirect_uris: Mapped[list[str]] = mapped_column(JSON())
    # The tool's JWK Set URL, verifying the client assertions it signs
    jwks_url: Mapped[str] = mapped_column(String(2000))
    # Whether launches share the learner's name and email with the tool;
    # otherwise the learner is identified by their user id only
    share_identity: Mapped[bool] = mapped_column(Boolean())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LtiLink(Base):
    """A resource link: one tool resource placed as an activity variant.

    The link owns one declarative line item (label = title), so scores the
    tool publishes for it drive the variant's satisfaction.
    """

    __tablename__ = "lms_lti_link"

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("lms_activity_variant.id"))
    tool_id: Mapped[int] = mapped_column(ForeignKey("lms_lti_tool.id"))
    # Platform-unique resource_link_id sent in every launch of this link
    resource_link_id: Mapped[str] = mapped_column(String(255), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    target_link_uri: Mapped[str] = mapped_column(String(2000))
    # Custom properties sent in the custom claim, string values only
    custom: Mapped[dict[str, str]] = mapped_column(JSON())
    score_maximum: Mapped[float] = mapped_column(Float())


class LtiLaunch(Base):
    """A learner's in-flight OIDC login for one link.

    The opaque ``message_hint`` travels to the tool as ``lti_message_hint``
    and comes back in the authentication request, binding it to the learner
    who started the launch. Single use: consumed by the authorization
    endpoint, and refused after ``expires_at``.
    """

    __tablename__ = "lms_lti_launch"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_hint: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("lms_lti_link.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lms_user.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LtiResult(Base):
    """The latest score a tool published for one link and enrollment."""

    __tablename__ = "lms_lti_result"
    __table_args__ = (UniqueConstraint("link_id", "enrollment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("lms_lti_link.id"))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("lms_enrollment.id", ondelete="CASCADE"))
    score_given: Mapped[float | None] = mapped_column(Float())
    score_maximum: Mapped[float | None] = mapped_column(Float())
    activity_progress: Mapped[str] = mapped_column(String(20))
    grading_progress: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(Text())
    # The tool's score timestamp; older updates are ignored (AGS 3.4.9)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LtiClientAssertion(Base):
    """A client assertion ``jti`` already accepted, refused on replay until
    the assertion expires."""

    __tablename__ = "lms_lti_client_assertion"
    __table_args__ = (UniqueConstraint("tool_id", "jti"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tool_id: Mapped[int] = mapped_column(ForeignKey("lms_lti_tool.id"))
    jti: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
