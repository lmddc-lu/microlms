"""Shared test helpers: out-of-band seeding, recording hooks, a fake LRS and content packages."""

import uuid
from pathlib import Path

from microlms import create_db_engine, create_session_factory
from microlms.api import CoreRouterName
from microlms.domain.auth_tokens import AnonymousToken
from microlms.domain.orgs import Org, get_org_by_slug
from microlms.domain.users import User, UserRole, role_assignments_for
from microlms.events import (
    CourseCompleted,
    CourseOrgAssigned,
    CourseOrgUnassigned,
    CoursePublished,
    EnrollmentDueSoon,
    PasswordResetRequested,
    RoleGranted,
    RoleRevoked,
    ScormRuntimeCommitted,
    UserCreated,
    UserEnrolled,
)

ALL_CORE_ROUTERS: list[CoreRouterName] = ["users", "orgs", "courses", "enrollments", "audit"]


DEFAULT_ORG_SLUG = "default"
# Dedicated organization xAPI guests are provisioned into
GUEST_ORG_SLUG = "guests"


def seed_admin(database_url: str) -> uuid.UUID:
    """Bootstrap an admin and the default organization out-of-band and return
    an anonymous token for the admin.

    Mirrors how a real deployment bootstraps: the anonymous and JWT
    registration flows only ever create learners, so admins are provisioned
    directly against the database, never through the public API, and the
    default organization self-registered learners join must exist.
    """
    engine = create_db_engine(database_url, echo=False)
    session_factory = create_session_factory(engine)
    token = uuid.uuid4()
    seed_default_org(database_url)
    with session_factory() as session:
        # An email with no password: the admin uses its token
        admin = User(
            full_name="Admin",
            email="admin@example.com",
            role_assignments=role_assignments_for({UserRole.ADMIN}),
        )
        session.add(admin)
        session.flush()
        session.add(AnonymousToken(token=token, user_id=admin.id))
        session.commit()
    engine.dispose()
    return token


def seed_org(database_url: str, slug: str) -> None:
    """Create an organization out-of-band, like a deployment bootstrap."""
    engine = create_db_engine(database_url, echo=False)
    with create_session_factory(engine)() as session:
        session.add(Org(slug=slug, name=slug))
        session.commit()
    engine.dispose()


def seed_default_org(database_url: str) -> None:
    """Create the default organization self-registered learners join."""
    seed_org(database_url, DEFAULT_ORG_SLUG)


def org_id_of(database_url: str, slug: str) -> int:
    engine = create_db_engine(database_url, echo=False)
    with create_session_factory(engine)() as session:
        org_id = get_org_by_slug(session, slug).id
    engine.dispose()
    return org_id


def default_org_id(database_url: str) -> int:
    """Id of the default organization seeded by :func:`seed_admin`."""
    return org_id_of(database_url, DEFAULT_ORG_SLUG)


def issue_token(database_url: str, user_id: str) -> uuid.UUID:
    """Issue an anonymous token out-of-band for an API-created user.

    Lets tests authenticate as users created through POST /api/v1/users, which
    issues no credential (a real deployment does that via its auth backend).
    """
    engine = create_db_engine(database_url, echo=False)
    session_factory = create_session_factory(engine)
    token = uuid.uuid4()
    with session_factory() as session:
        session.add(AnonymousToken(token=token, user_id=uuid.UUID(user_id)))
        session.commit()
    engine.dispose()
    return token


class RecordingHooks:
    """Hooks implementation that records every received event."""

    def __init__(self) -> None:
        self.created: list[UserCreated] = []
        self.enrolled: list[UserEnrolled] = []
        self.completed: list[CourseCompleted] = []
        self.due_soon: list[EnrollmentDueSoon] = []
        self.reset_requests: list[PasswordResetRequested] = []
        self.granted: list[RoleGranted] = []
        self.revoked: list[RoleRevoked] = []
        self.published: list[CoursePublished] = []
        self.org_assigned: list[CourseOrgAssigned] = []
        self.org_unassigned: list[CourseOrgUnassigned] = []
        self.scorm_commits: list[ScormRuntimeCommitted] = []

    def on_user_created(self, event: UserCreated) -> None:
        self.created.append(event)

    def on_user_enrolled(self, event: UserEnrolled) -> None:
        self.enrolled.append(event)

    def on_course_completed(self, event: CourseCompleted) -> None:
        self.completed.append(event)

    def on_enrollment_due_soon(self, event: EnrollmentDueSoon) -> None:
        self.due_soon.append(event)

    def on_password_reset_requested(self, event: PasswordResetRequested) -> None:
        self.reset_requests.append(event)

    def on_role_granted(self, event: RoleGranted) -> None:
        self.granted.append(event)

    def on_role_revoked(self, event: RoleRevoked) -> None:
        self.revoked.append(event)

    def on_course_published(self, event: CoursePublished) -> None:
        self.published.append(event)

    def on_course_org_assigned(self, event: CourseOrgAssigned) -> None:
        self.org_assigned.append(event)

    def on_course_org_unassigned(self, event: CourseOrgUnassigned) -> None:
        self.org_unassigned.append(event)

    def on_scorm_runtime_committed(self, event: ScormRuntimeCommitted) -> None:
        self.scorm_commits.append(event)


CMI5_XML = """<?xml version="1.0" encoding="utf-8"?>
<courseStructure xmlns="https://w3id.org/xapi/profiles/cmi5/v1/CourseStructure.xsd">
  <course id="https://example.com/course/geology">
    <title><langstring lang="en-US">Geology</langstring></title>
    <description><langstring lang="en-US">Intro to geology.</langstring></description>
  </course>
  <au id="https://example.com/course/geology/au1"
      moveOn="CompletedOrPassed" masteryScore="0.3" launchMethod="OwnWindow">
    <title><langstring lang="en-US">Geology AU</langstring></title>
    <description><langstring lang="en-US">The one unit.</langstring></description>
    <url>index.html</url>
  </au>
</courseStructure>
"""


def cmi5_zip() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("cmi5.xml", CMI5_XML)
        archive.writestr("index.html", "<html><body>GEOLOGY-AU-MARKER</body></html>")
    return buffer.getvalue()


class FakeLrs:
    """Records launch data and statements; forwards return a canned 200.

    Reads serve from the recorded statements, honoring the agent/verb/
    registration/activity filters (``related_activities`` also matching context
    activities); setting ``page_size`` splits the result into a first page
    plus a ``more`` page, to exercise paging.
    """

    def __init__(self) -> None:
        self.states: list[tuple[str, str, dict]] = []
        self.statements: list[dict] = []
        self.forwarded: list[tuple[str, str]] = []
        self.forwarded_queries: list[str] = []
        self.page_size: int | None = None
        self._pending: list[dict] = []

    def put_state(self, activity_id, agent, registration, state_id, document) -> None:
        self.states.append((activity_id, state_id, document))

    def post_statement(self, statement) -> None:
        self.statements.append(statement)

    def post_statements(self, statements) -> None:
        self.statements.extend(statements)

    def get_statements(self, params):
        import json

        from microlms.lrs import StatementsPage

        found = list(self.statements)
        if "agent" in params:
            name = json.loads(params["agent"])["account"]["name"]
            found = [s for s in found if s.get("actor", {}).get("account", {}).get("name") == name]
        if "verb" in params:
            found = [s for s in found if s.get("verb", {}).get("id") == params["verb"]]
        if "registration" in params:
            found = [
                s
                for s in found
                if s.get("context", {}).get("registration") == params["registration"]
            ]
        if "activity" in params:

            def _activity_ids(statement: dict) -> set[str]:
                ids = {statement.get("object", {}).get("id")}
                if params.get("related_activities") == "true":
                    context_activities = statement.get("context", {}).get("contextActivities", {})
                    for activities in context_activities.values():
                        ids |= {activity["id"] for activity in activities}
                return ids

            found = [s for s in found if params["activity"] in _activity_ids(s)]
        if self.page_size is not None and len(found) > self.page_size:
            self._pending = found[self.page_size :]
            return StatementsPage(
                statements=found[: self.page_size], more="/xapi/statements?from=next"
            )
        return StatementsPage(statements=found, more=None)

    def get_more(self, more_path):
        from microlms.lrs import StatementsPage

        assert more_path == "/xapi/statements?from=next"
        pending, self._pending = self._pending, []
        return StatementsPage(statements=pending, more=None)

    def forward(self, method, path, query, headers, body):
        import httpx

        self.forwarded.append((method, path))
        self.forwarded_queries.append(query)
        return httpx.Response(200, json=["stmt-id"])


def verb_ids(statements: list[dict]) -> set[str]:
    return {s["verb"]["id"] for s in statements}


SCORM_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest identifier="com.example.golf" version="1"
          xmlns="http://www.imsglobal.org/xsd/imscp_v1p1"
          xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_v1p3">
  <metadata>
    <schema>ADL SCORM</schema>
    <schemaversion>2004 3rd Edition</schemaversion>
  </metadata>
  <organizations default="org1">
    <organization identifier="org1">
      <title>Golf Explained</title>
      <item identifier="item_1" identifierref="resource_1" parameters="?lang=en">
        <title>Playing the Game</title>
      </item>
    </organization>
  </organizations>
  <resources>
    <resource identifier="resource_1" type="webcontent" adlcp:scormType="sco"
              href="index.html">
      <file href="index.html"/>
    </resource>
  </resources>
</manifest>
"""


def scorm_zip(manifest: str) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("imsmanifest.xml", manifest)
        archive.writestr("index.html", "<html><body>GOLF-SCO-MARKER</body></html>")
    return buffer.getvalue()


FIXTURES_DIR = Path(__file__).parent / "fixtures"
