"""cmi5 ORM models: imported AUs and launch sessions.

Core tables (``lms_`` prefix): the parsed Assignable Units of a cmi5 package
and the per-launch sessions that track a learner's progress through one AU.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from microlms.db.base import Base


class MoveOn(enum.StrEnum):
    """cmi5 satisfaction criteria for an AU (the ``moveOn`` attribute)."""

    PASSED = "Passed"
    COMPLETED = "Completed"
    COMPLETED_AND_PASSED = "CompletedAndPassed"
    COMPLETED_OR_PASSED = "CompletedOrPassed"
    NOT_APPLICABLE = "NotApplicable"


class LaunchMethod(enum.StrEnum):
    """cmi5 ``launchMethod`` attribute of an AU."""

    OWN_WINDOW = "OwnWindow"
    ANY_WINDOW = "AnyWindow"


class LaunchMode(enum.StrEnum):
    """cmi5 launch mode; only Normal records satisfaction data."""

    NORMAL = "Normal"
    BROWSE = "Browse"
    REVIEW = "Review"


class Cmi5Au(Base):
    """A cmi5 Assignable Unit parsed from a package's ``cmi5.xml``."""

    __tablename__ = "lms_cmi5_au"

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("lms_activity_variant.id"))
    au_index: Mapped[int] = mapped_column(Integer())
    # The AU 'id' attribute from the course structure (the xAPI activity id)
    publisher_id: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text())
    move_on: Mapped[MoveOn] = mapped_column(Enum(MoveOn, name="cmi5_move_on"))
    mastery_score: Mapped[float | None] = mapped_column(Float())
    launch_method: Mapped[LaunchMethod] = mapped_column(
        Enum(LaunchMethod, name="cmi5_launch_method")
    )
    # Launch URL relative to the package root (the AU's <url> element)
    launch_url: Mapped[str] = mapped_column(String(1000))
    launch_parameters: Mapped[str | None] = mapped_column(String(1000))
    # Storage key prefix under which the package files were stored
    storage_prefix: Mapped[str] = mapped_column(String(500))


class Cmi5Session(Base):
    """One launch of one AU by one learner; tracks cmi5 progress."""

    __tablename__ = "lms_cmi5_session"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    au_id: Mapped[int] = mapped_column(ForeignKey("lms_cmi5_au.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lms_user.id", ondelete="CASCADE"))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("lms_enrollment.id", ondelete="CASCADE"))
    registration: Mapped[uuid.UUID] = mapped_column(Uuid)
    # Opaque per-session credential the AU sends back as `Authorization: Basic`
    auth_token: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    # When the session stops serving content and relaying xAPI, at the latest
    # (Settings.launch_max_age after the launch); `terminated` ends it sooner
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    launch_mode: Mapped[LaunchMode] = mapped_column(Enum(LaunchMode, name="cmi5_launch_mode"))
    # One-time fetch guard: the fetch URL yields the auth token exactly once
    fetched: Mapped[bool] = mapped_column(Boolean())
    completed: Mapped[bool] = mapped_column(Boolean())
    passed: Mapped[bool | None] = mapped_column(Boolean())
    satisfied: Mapped[bool] = mapped_column(Boolean())
    terminated: Mapped[bool] = mapped_column(Boolean())
    # Latest cmi5 progress result extension the AU reported in this session,
    # as a fraction (0 to 1); None until it reports one
    progress_measure: Mapped[float | None] = mapped_column(Float())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
