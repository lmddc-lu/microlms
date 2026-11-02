"""Organization endpoints; orgs are platform-global resources, so every
action requires its permission from a platform assignment."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from microlms.api.deps import HooksDep, SessionDep, require_platform_permission
from microlms.authz import Permission
from microlms.domain.orgs import (
    OrgInUseError,
    OrgNotFoundError,
    SlugAlreadyUsedError,
    create_org,
    delete_org,
    get_org,
    list_orgs,
)
from microlms.events import commit_with_events

router = APIRouter(prefix="/orgs", tags=["orgs"])


class OrgCreate(BaseModel):
    slug: str
    name: str


class OrgRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str


@router.get("", dependencies=[Depends(require_platform_permission(Permission.ORG_READ))])
def read_orgs(session: SessionDep) -> list[OrgRead]:
    return [OrgRead.model_validate(org) for org in list_orgs(session)]


@router.get("/{org_id}", dependencies=[Depends(require_platform_permission(Permission.ORG_READ))])
def read_org(session: SessionDep, org_id: int) -> OrgRead:
    try:
        return OrgRead.model_validate(get_org(session, org_id))
    except OrgNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_platform_permission(Permission.ORG_CREATE))],
)
def add_org(session: SessionDep, hooks: HooksDep, payload: OrgCreate) -> OrgRead:
    try:
        org = create_org(session, slug=payload.slug, name=payload.name)
    except SlugAlreadyUsedError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    commit_with_events(session, hooks)
    return OrgRead.model_validate(org)


@router.delete(
    "/{org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_platform_permission(Permission.ORG_DELETE))],
)
def remove_org(session: SessionDep, hooks: HooksDep, org_id: int) -> None:
    try:
        delete_org(session, org_id)
    except OrgNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except OrgInUseError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    commit_with_events(session, hooks)
