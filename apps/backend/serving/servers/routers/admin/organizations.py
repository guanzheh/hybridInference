"""Admin routes that set an organization up, suspend it, restore it and delete it.

The platform administrator makes an organization, attaches its SSO
connections, gives the role of organization administrator and sets the
organization's monthly limit. What an organization administrator then does in
its own organization is in :mod:`serving.servers.routers.organization`.

A suspend writes the status and nothing else: the gateway reads it where it
authenticates an account (:func:`serving.servers.deps.refuse_suspended_organization`),
so a restore brings everything back. A delete cannot be undone.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving import organizations
from serving.accounting import get_accounting_policy
from serving.audit import AuditActor  # noqa: TC001 (FastAPI reads the annotation)
from serving.auth import sso_accounts
from serving.config import sso_switch
from serving.schemas_organization import (
    ConnectionAttachedResponse,
    CreateOrganizationRequest,
    DeleteOrganizationRequest,
    ListOrganizationsResponse,
    OrganizationCreatedResponse,
    OrganizationDeletedResponse,
    OrganizationMessageResponse,
    OrganizationResponse,
    SetMemberRoleRequest,
    UpdateOrganizationRequest,
)
from serving.servers.deps import (
    get_admin_audit_actor,
    get_admin_user_id,
    get_batch_store,
    get_operational_store,
    verify_admin_access,
)
from serving.servers.routers.admin.config import log_config_changes
from serving.servers.routers.admin.reauthentication import confirm_administrator
from serving.storage import organizations as org_store
from serving.storage.batch_store import PAUSE_ORGANIZATION_SUSPENDED
from serving.utils.jwt import generate_ulid
from serving.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin")


def _refusal(status_code: int, code: str, message: str) -> HTTPException:
    """A refusal the console tells apart by its code: ``{"error": {"type", "code", "message"}}``.

    A delete has several causes that share a status, and the console says a
    different thing for each.
    """
    return HTTPException(
        status_code, {"error": {"type": "organization_error", "code": code, "message": message}}
    )


def _store(op_store: Any) -> Any:
    if not op_store:
        raise HTTPException(500, "Database not configured")
    return op_store


def _native_limit(payload: Any) -> Any:
    """The native monthly limit a request sent with the accounting ``currency``, exactly."""
    if get_accounting_policy().native and "monthly_limit" in payload.model_fields_set:
        return payload.monthly_limit
    return None


async def _organization(op_store: Any, organization_id: str) -> OrganizationResponse:
    view = await organizations.organization_view(op_store, organization_id)
    if view is None:
        raise HTTPException(404, "No such organization.")
    return OrganizationResponse(**view)


def _provider(provider_id: str) -> Any:
    provider = organizations.configured_provider(provider_id)
    if provider is None:
        raise HTTPException(404, f"SSO_PROVIDERS has no provider with the id {provider_id!r}.")
    return provider


@router.get("/organizations", response_model=ListOrganizationsResponse)
async def list_organizations(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListOrganizationsResponse:
    """Every organization of the deployment, by name."""
    store = _store(op_store)
    rows = await store.list_organizations()
    return ListOrganizationsResponse(
        organizations=[await _organization(store, row["id"]) for row in rows]
    )


@router.post("/organizations", response_model=OrganizationCreatedResponse, status_code=201)
async def create_organization(
    payload: CreateOrganizationRequest,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationCreatedResponse:
    """Make an organization from one SSO connection or more.

    Each account that has an identity of one of the connections and an email
    in its domains becomes a member.

    Raises:
        HTTPException: 404 for a provider id ``SSO_PROVIDERS`` does not have;
            409 when the name is another organization's, or a connection
            belongs to another organization.
    """
    store = _store(op_store)
    provider_ids = list(dict.fromkeys(payload.provider_ids))
    connections = [
        (provider_id, partial(organizations.accounts_of_connection, _provider(provider_id)))
        for provider_id in provider_ids
    ]
    organization_id = generate_ulid()
    outcome, added = await store.create_organization(
        organization_id=organization_id,
        name=payload.name,
        monthly_limit_usd=payload.monthly_limit_usd,
        monthly_limit_amount=_native_limit(payload),
        connections=connections,
        actor=actor,
    )
    if outcome == org_store.NAME_TAKEN:
        raise HTTPException(409, "Another organization has this name.")
    if outcome == org_store.CONNECTION_TAKEN:
        raise HTTPException(409, "One of these SSO connections belongs to another organization.")
    return OrganizationCreatedResponse(
        organization=await _organization(store, organization_id), members_added=added
    )


@router.get("/organizations/{organization_id}", response_model=OrganizationResponse)
async def get_organization(
    organization_id: str,
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> OrganizationResponse:
    """One organization, in the shape its own administrators see it."""
    return await _organization(_store(op_store), organization_id)


@router.patch("/organizations/{organization_id}", response_model=OrganizationResponse)
async def update_organization(
    organization_id: str,
    payload: UpdateOrganizationRequest,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationResponse:
    """Change the name or the monthly limit; a field that is not sent stays as it is.

    Raises:
        HTTPException: 404 for no such organization; 409 when the name is
            another organization's.
    """
    store = _store(op_store)
    sent = payload.model_fields_set
    if "name" in sent and payload.name is not None:
        outcome = await store.rename_organization(organization_id, payload.name, actor=actor)
        if outcome == org_store.NOT_FOUND:
            raise HTTPException(404, "No such organization.")
        if outcome == org_store.NAME_TAKEN:
            raise HTTPException(409, "Another organization has this name.")
    if sent & {"monthly_limit_usd", "monthly_limit"}:
        outcome, _old, _new = await store.set_organization_limit(
            organization_id,
            "monthly_limit",
            usd=payload.monthly_limit_usd,
            amount=_native_limit(payload),
            action="organization.limit.set",
            actor=actor,
        )
        if outcome == org_store.NOT_FOUND:
            raise HTTPException(404, "No such organization.")
    return await _organization(store, organization_id)


@router.post("/organizations/{organization_id}/suspend", response_model=OrganizationResponse)
async def suspend_organization(
    organization_id: str,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationResponse:
    """Suspend an organization: the gateway refuses its members from their next request on.

    Every member that is not a platform administrator (the ``admin`` role, or
    an ``ADMIN_EMAILS`` address), on every route that takes an API key, a
    grant token or a session, and at sign-in.
    Only the status is written: the accounts, the keys and the sessions stay
    as they are, and a restore brings them back. An organization that is
    suspended already is answered the same, with no audit row.

    Raises:
        HTTPException: 404 for no such organization.
    """
    store = _store(op_store)
    outcome = await store.set_organization_status(organization_id, "suspended", actor=actor)
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, "No such organization.")
    return await _organization(store, organization_id)


@router.post("/organizations/{organization_id}/restore", response_model=OrganizationResponse)
async def restore_organization(
    organization_id: str,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
    batch_store=Depends(get_batch_store),
) -> OrganizationResponse:
    """Restore a suspended organization: the same keys and the open sessions work again.

    Then the batches of its members that the suspension paused continue. That
    is a second step, in the batch store: if it fails, the organization is
    active, and those batches continue when their pause ends. An organization
    that is active already is answered the same, with no audit row, and the
    second step is done again.

    Raises:
        HTTPException: 404 for no such organization.
    """
    store = _store(op_store)
    outcome = await store.set_organization_status(organization_id, "active", actor=actor)
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, "No such organization.")
    await _continue_batches(store, batch_store, organization_id)
    return await _organization(store, organization_id)


async def _continue_batches(store: Any, batch_store: Any, organization_id: str) -> None:
    """Lift the pause of every batch that waits for this organization to be restored.

    Never raises: the restore is done, and a batch whose pause could not be
    lifted continues when the pause ends.
    """
    if batch_store is None:
        return
    try:
        member_ids = await store.list_organization_member_ids(organization_id)
        continued = await batch_store.clear_user_batch_pauses(
            member_ids, PAUSE_ORGANIZATION_SUSPENDED
        )
    except Exception:
        logger.warning(
            "organization %s was restored, but the pause of its members' batches could not be "
            "lifted; they continue when it ends",
            organization_id,
            exc_info=True,
        )
        return
    if continued:
        logger.info(
            "organization %s restored: %d paused batch(es) continue", organization_id, continued
        )


_NOT_DELETED_CONNECTIONS_OFF = (
    "The organization was not deleted, and its SSO connections are switched off. Delete it "
    "again, or switch them on in Admin Settings."
)
_NOT_DELETED_CONNECTIONS_UNKNOWN = (
    "The organization was not deleted. Its SSO connections may be switched off: read "
    "SSO_PROVIDERS in Admin Settings, then delete it again."
)


@router.delete("/organizations/{organization_id}", response_model=OrganizationDeletedResponse)
async def delete_organization(
    organization_id: str,
    payload: DeleteOrganizationRequest,
    request: Request,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationDeletedResponse:
    """Delete an organization. This cannot be undone.

    The member accounts that are active are suspended (their sessions end and
    their keys are revoked), except a platform administrator and an account
    that waits for approval. The organization goes with its
    memberships, connections and counters. The accounts, their usage and the
    audit rows stay.

    With ``disable_connections`` (the default) the ``SSO_PROVIDERS`` entries of
    its connections are switched off first, so that a person who signs in
    through one for the first time gets no personal account. That is a write
    to the settings, not part of the delete's transaction; it goes first,
    because an entry can be switched on again and a delete cannot be undone.

    The checks, in this order, and nothing is written when one refuses: the
    organization exists; ``confirm_name`` is its name; with
    ``disable_connections``, the settings can be changed and the caller keeps
    a way to sign in; the administrator confirms. The confirmation is last,
    so that a request refused for another reason spends none.

    Raises:
        HTTPException: 404 for no such organization. 409 with a ``code``:
            ``confirm_name_mismatch``; ``settings_invalid`` when
            ``SSO_PROVIDERS`` does not validate; ``last_sign_in`` when the
            caller signs in only through one of the connections;
            ``settings_changed`` when the settings were saved meanwhile;
            ``organization_changed`` when the organization got another name or
            another connection meanwhile. 400 or 403 when the administrator
            does not confirm (403 for the admin token). 500
            ``delete_incomplete`` when the delete failed after it started to
            switch the connections off: the message says if they are off, or
            if they can be off. 503 ``settings_unavailable`` when the settings
            cannot be read, and the status the settings gave with
            ``settings_refused`` when they refuse the write for another reason.
    """
    store = _store(op_store)
    organization = await store.get_organization(organization_id)
    if organization is None:
        raise HTTPException(404, "No such organization.")
    name = organization["name"]
    if payload.confirm_name != name:
        raise _refusal(
            409, "confirm_name_mismatch", "The name does not match the name of the organization."
        )

    connections = list(organization["provider_ids"])
    version, to_switch = None, []
    if payload.disable_connections:
        try:
            version = await sso_switch.read_version()
        except sso_switch.SsoSwitchError as exc:
            raise _refusal(exc.status_code, exc.code, exc.message) from None
        to_switch = sso_switch.entries_on(connections)
        await _keep_the_caller_signed_in(request, store, to_switch)

    await confirm_administrator(
        request, store, payload.password, action=f"Deleting the organization {name}"
    )

    disabled: list[str] = []
    if to_switch:
        try:
            disabled, changes = await sso_switch.switch_off(
                to_switch, version=version, updated_by=admin_id
            )
        except sso_switch.SsoSwitchError as exc:
            raise _refusal(exc.status_code, exc.code, exc.message) from None
        except Exception:
            # Not a refusal of the settings: they can have stored the change
            # and failed after it (the reload follows the commit). So the
            # answer cannot say that nothing changed.
            logger.exception("organization %s: delete stopped at its connections", organization_id)
            raise _refusal(500, "delete_incomplete", _NOT_DELETED_CONNECTIONS_UNKNOWN) from None
        try:
            await log_config_changes(store, changes, actor=actor)
        except Exception:
            logger.exception(
                "organization %s: delete stopped after its connections", organization_id
            )
            raise _refusal(500, "delete_incomplete", _NOT_DELETED_CONNECTIONS_OFF) from None

    try:
        outcome, result = await store.delete_organization(
            organization_id,
            confirm_name=payload.confirm_name,
            handled_connections=connections if payload.disable_connections else None,
            disabled_connections=disabled,
            suspension_message=f"The organization {name} was deleted.",
            actor=actor,
        )
    except Exception:
        if not disabled:
            raise
        logger.exception("organization %s: delete failed after its connections", organization_id)
        raise _refusal(500, "delete_incomplete", _NOT_DELETED_CONNECTIONS_OFF) from None
    if outcome == org_store.NOT_FOUND:
        # Another delete of this organization finished first.
        raise HTTPException(404, "No such organization.")
    if outcome != org_store.DELETED:
        changed = (
            "The organization got another name"
            if outcome == org_store.NAME_CHANGED
            else "The organization got another SSO connection"
        )
        note = " Its SSO connections are switched off." if disabled else ""
        raise _refusal(
            409,
            "organization_changed",
            f"{changed} while it was deleted. Nothing was deleted.{note} Read it again, then "
            "delete again.",
        )
    return OrganizationDeletedResponse(
        message=f"The organization {name} was deleted.",
        accounts_suspended=len(result["suspended_user_ids"]),
        connections_disabled=disabled,
    )


async def _keep_the_caller_signed_in(request: Request, store: Any, to_switch: list[str]) -> None:
    """Refuse a delete that would switch off the only way its caller signs in.

    A platform administrator who signs in through a connection of the
    organization, and has no password, no verified phone and no identity of an
    entry that stays on (:func:`serving.auth.sso_accounts.loses_sign_in`). An
    admin token has no account: it passes here and is refused at the
    confirmation.

    Raises:
        HTTPException: 409 ``last_sign_in``.
    """
    user_id = get_admin_user_id(request)
    if user_id is None or not to_switch:
        return
    user = await store.get_user_by_id(user_id)
    if user and await sso_accounts.loses_sign_in(store, user, to_switch):
        raise _refusal(
            409,
            "last_sign_in",
            "You sign in through an SSO connection of this organization and have no other way "
            "to sign in. Set a password first, or keep the SSO connections on.",
        )


@router.put(
    "/organizations/{organization_id}/connections/{provider_id}",
    response_model=ConnectionAttachedResponse,
)
async def attach_connection(
    organization_id: str,
    provider_id: str,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> ConnectionAttachedResponse:
    """Attach an SSO connection to an organization.

    Each account that has an identity of the connection and an email in its
    domains, and is in no organization, becomes a member. Attaching a
    connection the organization already has adds the members it lacks.

    Raises:
        HTTPException: 404 for no such organization or provider; 409 when the
            connection belongs to another organization.
    """
    store = _store(op_store)
    outcome, added = await store.attach_organization_connection(
        organization_id,
        provider_id,
        accounts_of=partial(organizations.accounts_of_connection, _provider(provider_id)),
        actor=actor,
    )
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, "No such organization.")
    if outcome == org_store.CONNECTION_TAKEN:
        raise HTTPException(409, "This SSO connection belongs to another organization.")
    return ConnectionAttachedResponse(
        organization=await _organization(store, organization_id), members_added=added
    )


@router.delete(
    "/organizations/{organization_id}/connections/{provider_id}",
    response_model=OrganizationResponse,
)
async def detach_connection(
    organization_id: str,
    provider_id: str,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationResponse:
    """Detach an SSO connection. The members stay.

    New accounts of the connection no longer become members. A connection
    whose provider ``SSO_PROVIDERS`` no longer has is detached like any other.

    Raises:
        HTTPException: 404 when the organization has no such connection.
    """
    store = _store(op_store)
    if not await store.detach_organization_connection(organization_id, provider_id, actor=actor):
        raise HTTPException(404, "This organization has no such SSO connection.")
    return await _organization(store, organization_id)


@router.put(
    "/organizations/{organization_id}/members/{user_id}/role",
    response_model=OrganizationMessageResponse,
)
async def set_member_role(
    organization_id: str,
    user_id: str,
    payload: SetMemberRoleRequest,
    request: Request,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationMessageResponse:
    """Give a member the role of organization administrator, or take it back.

    The account must be a member already: the first administrator signs in
    through an SSO connection of the organization once before this.

    Giving the role asks the administrator to confirm (a recent code, or the
    password of an account without an authenticator), as raising the role of
    an account does: its holder sees the usage of every member and can suspend
    them, and keeps the role after this session ends. Taking the role back
    asks for nothing.

    Raises:
        HTTPException: 404 when the account is not a member of the
            organization; 400 or 403 when the administrator does not confirm
            (403 for the admin token, which has no account to confirm with).
    """
    store = _store(op_store)
    if payload.role == "admin":
        membership = await store.get_organization_membership(user_id)
        if membership is None or membership["organization_id"] != organization_id:
            raise HTTPException(404, "This account is not a member of the organization.")
        if membership["role"] != "admin":
            # Last of the checks, so a request refused for another reason
            # spends no sign-in attempt.
            await confirm_administrator(
                request,
                store,
                payload.password,
                action=(
                    "Giving the administrator role of the organization "
                    f"{membership['organization_name']}"
                ),
            )
    outcome = await store.set_organization_member_role(
        organization_id, user_id, payload.role, actor=actor
    )
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, "This account is not a member of the organization.")
    if payload.role == "admin":
        return OrganizationMessageResponse(message="The member is an organization administrator.")
    return OrganizationMessageResponse(message="The member is no longer an administrator.")
