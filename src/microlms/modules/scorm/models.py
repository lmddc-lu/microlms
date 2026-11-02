"""SCORM ORM models: imported SCOs and learner attempts.

Core tables (``lms_`` prefix): the launchable items of a SCORM package
(1.2 or 2004) and the per-enrollment attempts that persist a learner's
runtime data (cmi) for one SCO. One attempt row per (SCO, enrollment) is
reused across launches, so relaunching resumes with the previously committed
data. Attempts store the SCORM 2004 data model; the player normalizes SCORM
1.2 runtime data (``cmi.core.*``) into it before reporting.
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
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from microlms.db.base import Base


class ScormVersion(enum.StrEnum):
    """The SCORM edition a package declares in its manifest ``<schemaversion>``."""

    SCORM_12 = "1.2"
    SCORM_2004 = "2004"


class ScormResourceType(enum.StrEnum):
    """The ``adlcp:scormType`` of the resource an item launches."""

    SCO = "sco"
    ASSET = "asset"


class CompletionStatus(enum.StrEnum):
    """SCORM 2004 ``cmi.completion_status`` vocabulary."""

    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    NOT_ATTEMPTED = "not attempted"
    UNKNOWN = "unknown"


class SuccessStatus(enum.StrEnum):
    """SCORM 2004 ``cmi.success_status`` vocabulary."""

    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ScormSco(Base):
    """A launchable item parsed from a SCORM package's ``imsmanifest.xml``."""

    __tablename__ = "lms_scorm_sco"

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("lms_activity_variant.id"))
    # The package's SCORM edition, shared by all SCOs of the variant; decides
    # which runtime API the player exposes (API vs API_1484_11)
    version: Mapped[ScormVersion] = mapped_column(Enum(ScormVersion, name="scorm_version"))
    sco_index: Mapped[int] = mapped_column(Integer())
    # The <item> identifier from the manifest's organization tree
    identifier: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    scorm_type: Mapped[ScormResourceType] = mapped_column(
        Enum(ScormResourceType, name="scorm_resource_type")
    )
    # Launch URL relative to the package root: resource href (with xml:base
    # applied) plus the item's launch parameters
    launch_url: Mapped[str] = mapped_column(String(1000))
    # xAPI activity IRIs derived from the manifest and item identifiers
    activity_id: Mapped[str] = mapped_column(String(500))
    course_activity_id: Mapped[str] = mapped_column(String(500))
    # Storage key prefix under which the package files were stored
    storage_prefix: Mapped[str] = mapped_column(String(500))


class ScormAttempt(Base):
    """One learner's persistent runtime data for one SCO of an enrollment."""

    __tablename__ = "lms_scorm_attempt"
    __table_args__ = (UniqueConstraint("sco_id", "enrollment_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    sco_id: Mapped[int] = mapped_column(ForeignKey("lms_scorm_sco.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lms_user.id", ondelete="CASCADE"))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("lms_enrollment.id", ondelete="CASCADE"))
    # Opaque per-launch credential the player sends on runtime calls; rotated
    # on every launch so stale player windows lose access
    auth_token: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    # When the player token stops working: Settings.launch_max_age after the
    # launch, or the Terminate that ended its session
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completion_status: Mapped[CompletionStatus] = mapped_column(
        Enum(CompletionStatus, name="scorm_completion_status")
    )
    success_status: Mapped[SuccessStatus] = mapped_column(
        Enum(SuccessStatus, name="scorm_success_status")
    )
    score_scaled: Mapped[float | None] = mapped_column(Float())
    score_raw: Mapped[float | None] = mapped_column(Float())
    # cmi.progress_measure (0 to 1), restored on resume; SCORM 1.2 has none
    progress_measure: Mapped[float | None] = mapped_column(Float())
    # cmi.location bookmark and cmi.suspend_data, restored on resume
    location: Mapped[str | None] = mapped_column(String(1000))
    suspend_data: Mapped[str | None] = mapped_column(Text())
    # Accumulated cmi.session_time across terminated sessions, in seconds
    total_time_seconds: Mapped[float] = mapped_column(Float())
    # Whether the last session ended with cmi.exit = "suspend" (entry=resume)
    suspended: Mapped[bool] = mapped_column(Boolean())
    # Sticky completed-or-passed marker used for enrollment completion
    satisfied: Mapped[bool] = mapped_column(Boolean())
    # How many cmi.interactions entries of the current session were already
    # converted to `responded` statements; reset on every launch because the
    # runtime interactions array starts empty each session
    interactions_reported: Mapped[int] = mapped_column(Integer())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
