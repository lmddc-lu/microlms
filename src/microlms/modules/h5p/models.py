"""H5P ORM models: imported content items and learner attempts.

Core tables (``lms_`` prefix): the single content item an ``.h5p`` package
holds and the per-enrollment attempts that track a learner's completion of
it. One attempt row per (content, enrollment) is reused across launches with
a rotated token, so stale player windows lose access.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from microlms.db.base import Base


class H5pContent(Base):
    """The content item of an imported ``.h5p`` package."""

    __tablename__ = "lms_h5p_content"

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("lms_activity_variant.id"))
    title: Mapped[str] = mapped_column(String(500))
    # The h5p.json mainLibrary machine name (e.g. "H5P.InteractiveVideo")
    main_library: Mapped[str] = mapped_column(String(200))
    # Canonical xAPI activity IRI minted at import; the player stamps it as
    # the statement object id so statements never carry the player page URL
    activity_iri: Mapped[str] = mapped_column(String(500))
    # Storage key prefix under which the package files were stored
    storage_prefix: Mapped[str] = mapped_column(String(500))


class H5pAttempt(Base):
    """One learner's completion state for one content item of an enrollment."""

    __tablename__ = "lms_h5p_attempt"
    __table_args__ = (UniqueConstraint("content_id", "enrollment_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("lms_h5p_content.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lms_user.id", ondelete="CASCADE"))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("lms_enrollment.id", ondelete="CASCADE"))
    # Opaque per-launch credential the player sends on runtime calls; rotated
    # on every launch so stale player windows lose access
    auth_token: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    # When the player token stops working: Settings.launch_max_age after the
    # launch (H5P content reports no end of session)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Sticky completion marker used for enrollment completion
    satisfied: Mapped[bool] = mapped_column(Boolean())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
