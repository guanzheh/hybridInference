"""The refusal of a member of a suspended organization, where no database is needed.

What the store answers for whom is shown against PostgreSQL
(``tests/integration/storage/test_organization_status_store_postgres.py``), and
each surface over HTTP (``tests/integration/test_organization_status_postgres.py``).
Here: what a place that authenticates does with that answer -- the body of the
refusal, what is logged, what is not counted, and that a grant and a stored
key carry it to their callers.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from serving import grants, organizations
from serving.grant_auth import (
    AgentModelAuthError,
    AgentOrganizationSuspended,
    authenticate_grant_model_call,
)
from serving.servers import auth, deps
from serving.servers.routers import anthropic_messages
from serving.servers.routers.admin import organizations as admin_organizations
from serving.storage.base import OrganizationsUnavailableError
from serving.storage.batch_store import PAUSE_ORGANIZATION_SUSPENDED
from tests.unit import test_grant_model_auth as grant_tests

_signing_secret = grant_tests._signing_secret
store = grant_tests.store
token = grant_tests.token

SUSPENDED = {"organization_id": "o1", "organization_name": "Example University"}
MESSAGE = (
    "Organization 'Example University' is suspended. Contact an administrator of your organization."
)
ROW = {
    "id": 7,
    "user_id": "member-1",
    "role": "pro",
    "email": "m@example.edu",
    "email_verified": True,
    **SUSPENDED,
    "organization_suspended": True,
}
# Built at runtime: a literal in the shape of a key would be flagged as a secret.
KEY = "hyi-" + "k" * 32


def _request(path: str = "/v1/chat/completions") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("203.0.113.9", 4000),
        }
    )


@pytest.fixture
def door(monkeypatch):
    """The key authentication with its side effects recorded, and no IP block."""
    seen: dict[str, list[Any]] = {"rejections": [], "failures": []}

    async def not_blocked(_ip_info):
        return False, 0

    async def log_rejection(**fields):
        seen["rejections"].append(fields)

    async def record_auth_failure(ip_info):
        seen["failures"].append(ip_info)

    monkeypatch.setattr(auth, "is_ip_blocked", not_blocked)
    monkeypatch.setattr(auth, "log_rejection", log_rejection)
    monkeypatch.setattr(auth, "record_auth_failure", record_auth_failure)
    monkeypatch.setattr(auth.detached_tasks, "rejection_log", _run_now(seen))
    monkeypatch.setattr(auth, "hash_api_key", lambda key: f"hash-of-{key}")
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: True)
    yield seen
    for coroutine in seen.pop("pending", []):
        coroutine.close()


def _run_now(seen: dict[str, list[Any]]):
    def run(coroutine):
        seen.setdefault("pending", []).append(coroutine)

    return run


async def _drain(seen: dict[str, list[Any]]) -> None:
    for coroutine in seen.pop("pending", []):
        await coroutine


def _key_store(row: dict[str, Any] | None) -> MagicMock:
    op_store = MagicMock()
    op_store.get_auth_context_by_key_hash = AsyncMock(return_value=row)
    op_store.get_auth_context_lightweight = AsyncMock(return_value=row)
    op_store.get_user_cost_today = AsyncMock(return_value=0.0)
    return op_store


# -- the answer, and what is recorded ----------------------------------------------


def test_the_refusal_names_the_organization_and_has_a_code(monkeypatch) -> None:
    monkeypatch.setattr(deps, "get_site_identity", lambda: MagicMock(support_email="help@x.edu"))

    with pytest.raises(HTTPException) as refused:
        deps.refuse_suspended_organization(SUSPENDED)
    with pytest.raises(HTTPException) as plain:
        deps.refuse_suspended_organization(SUSPENDED, plain=True)

    assert (refused.value.status_code, refused.value.detail) == (
        403,
        {
            "error": "Organization suspended",
            "code": "organization_suspended",
            "organization": "Example University",
            "contact_email": "help@x.edu",
            "message": MESSAGE,
        },
    )
    # A session gets the form of the answer for an account that is not active.
    assert (plain.value.status_code, plain.value.detail) == (403, MESSAGE)
    # No organization, no refusal.
    assert deps.refuse_suspended_organization(None) is None


def test_a_row_says_so_only_with_a_true_flag() -> None:
    assert organizations.suspended_in(ROW) == SUSPENDED
    for row in (
        None,
        {},
        {**SUSPENDED},
        {**SUSPENDED, "organization_suspended": False},
        {**SUSPENDED, "organization_suspended": None},
        # A test double answers every attribute: it is not the store's True.
        {**SUSPENDED, "organization_suspended": MagicMock()},
    ):
        assert organizations.suspended_in(row) is None
    # The flag travels with the other organization columns of an identity.
    assert organizations.columns_of(ROW)["organization_suspended"] is True


@pytest.mark.asyncio
async def test_a_key_of_a_member_is_refused_logged_and_not_counted_as_a_failure(door) -> None:
    op_store = _key_store(dict(ROW))

    with pytest.raises(HTTPException) as refused:
        await auth._authenticate_by_api_key(_request(), f"Bearer {KEY}", None, op_store)
    await _drain(door)

    assert refused.value.status_code == 403
    assert refused.value.detail["code"] == "organization_suspended"
    assert refused.value.detail["organization"] == "Example University"
    # A live key was presented: the address is not counted toward the IP block.
    assert door["failures"] == []
    [row] = door["rejections"]
    assert (row["status_code"], row["error_code"]) == (403, "organization_suspended")
    assert row["reason"] == "organization_id=o1"
    assert row["user"] == {
        "user_id": "member-1",
        "role": "pro",
        "authenticated": True,
        "credential_state": "organization_suspended",
    }


@pytest.mark.asyncio
async def test_a_key_of_an_account_that_is_not_refused_passes(door) -> None:
    row = {**ROW, "organization_suspended": False}

    user, key_hash, _spend = await auth._authenticate_by_api_key(
        _request(), f"Bearer {KEY}", None, _key_store(row)
    )

    assert (user["user_id"], key_hash) == ("member-1", f"hash-of-{KEY}")
    assert door["rejections"] == []


@pytest.mark.asyncio
async def test_the_model_listing_refuses_a_member_and_is_anonymous_for_an_unknown_key(door) -> None:
    with pytest.raises(HTTPException) as refused:
        await auth.optional_verify_api_key(
            _request("/v1/models"),
            authorization=f"Bearer {KEY}",
            x_api_key=None,
            op_store=_key_store(dict(ROW)),
        )
    anonymous = await auth.optional_verify_api_key(
        _request("/v1/models"),
        authorization=f"Bearer {KEY}",
        x_api_key=None,
        op_store=_key_store(None),
    )

    assert (refused.value.status_code, refused.value.detail["code"]) == (
        403,
        "organization_suspended",
    )
    assert anonymous is None


@pytest.mark.parametrize(
    ("row", "state"),
    [
        ({"key_status": "active", "user_status": "active"}, "active"),
        (
            {"key_status": "active", "user_status": "active", "organization_suspended": True},
            "organization_suspended",
        ),
        # The account, the key and its deadline outrank the organization: each
        # of them is the blocker to clear first.
        (
            {"key_status": "active", "user_status": "suspended", "organization_suspended": True},
            "user_suspended",
        ),
        (
            {"key_status": "revoked", "user_status": "active", "organization_suspended": True},
            "revoked",
        ),
        (
            {
                "key_status": "active",
                "user_status": "active",
                "key_expired": True,
                "organization_suspended": True,
            },
            "expired",
        ),
    ],
)
def test_the_credential_state_of_a_blocked_address_names_the_organization(row, state) -> None:
    assert auth._credential_state(row) == state


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/messages", "/anthropic/v1/messages"])
async def test_an_anthropic_route_gives_the_refusal_in_the_anthropic_form(
    path, monkeypatch
) -> None:
    """Those routes re-wrap every refusal: the body is theirs, and it names no organization."""
    monkeypatch.setattr(deps, "get_site_identity", lambda: MagicMock(support_email=""))
    with pytest.raises(HTTPException) as refused:
        deps.refuse_suspended_organization(SUSPENDED)

    answer = await anthropic_messages.anthropic_aware_http_exception_handler(
        _request(path), refused.value
    )
    elsewhere = await anthropic_messages.anthropic_aware_http_exception_handler(
        _request("/v1/chat/completions"), refused.value
    )

    assert answer.status_code == 403
    assert json.loads(answer.body) == {
        "type": "error",
        "error": {"type": "permission_error", "message": "Organization suspended"},
    }
    # Every other route gives the body as it was built.
    assert json.loads(elsewhere.body)["organization"] == "Example University"
    assert json.loads(elsewhere.body)["code"] == "organization_suspended"


# -- a grant ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_grant_model_call_of_a_member_is_refused_before_its_quota(store, token) -> None:
    store.keys[0].update(SUSPENDED, organization_suspended=True)
    # Also over its quota: the organization is the refusal that is given.
    store.spend = 1_000.0

    with pytest.raises(AgentOrganizationSuspended) as refused:
        await authenticate_grant_model_call(token, op_store=store)

    assert isinstance(refused.value, AgentModelAuthError)
    assert refused.value.status_code == 403
    assert refused.value.organization == SUSPENDED
    assert refused.value.message == MESSAGE
    # The owner, for the row that records the refusal.
    assert (refused.value.user_id, refused.value.role) == ("user_1", "pro")


@pytest.mark.asyncio
async def test_a_grant_model_call_of_an_account_that_is_not_refused_passes(store, token) -> None:
    store.keys[0].update(SUSPENDED, organization_suspended=False)

    context = await authenticate_grant_model_call(token, op_store=store)

    assert context["user_id"] == "user_1"


@pytest.mark.asyncio
async def test_a_grant_gets_the_refusal_that_a_key_of_the_same_account_gets(
    door, monkeypatch
) -> None:
    async def refuse(_token, *, op_store):
        raise AgentOrganizationSuspended(SUSPENDED, user_id="owner-1", role="pro")

    monkeypatch.setattr(auth, "authenticate_grant_model_call", refuse)
    grant = f"{grants._TOKEN_PREFIX}." + "x" * 24

    with pytest.raises(HTTPException) as refused:
        await auth.verify_api_key(
            _request(),
            authorization=f"Bearer {grant}",
            x_api_key=None,
            x_on_behalf_of=None,
            op_store=object(),
            log_store=None,
        )
    await _drain(door)

    assert refused.value.status_code == 403
    assert refused.value.detail["code"] == "organization_suspended"
    assert refused.value.detail["message"] == MESSAGE
    [row] = door["rejections"]
    assert row["error_code"] == "organization_suspended"
    # The row is the owner's: a refusal that names nobody cannot be found again.
    assert (row["user"]["user_id"], row["user"]["role"]) == ("owner-1", "pro")


# -- a stored key --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stored_key_of_a_member_raises_a_signal_of_its_own(monkeypatch) -> None:
    """Not ``StoredKeyRejected``, which fails the work: a restore undoes a suspension."""
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: True)
    op_store = _key_store(dict(ROW))

    with pytest.raises(auth.StoredKeyOrganizationSuspended) as refused:
        await auth.authenticate_stored_key("owner-key-hash", op_store)

    assert not isinstance(refused.value, auth.StoredKeyRejected)
    assert refused.value.organization == SUSPENDED
    assert str(refused.value) == MESSAGE


# -- an account, read by its id ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_account_is_available_when_it_is_active_and_no_organization_refuses_it() -> None:
    op_store = MagicMock()
    op_store.get_suspended_organization = AsyncMock(return_value=None)
    active = {"id": "u1", "status": "active"}

    assert await organizations.account_available(op_store, active)
    assert not await organizations.account_available(op_store, None)
    assert not await organizations.account_available(op_store, {"id": "u1", "status": "suspended"})
    op_store.get_suspended_organization.assert_awaited_once_with("u1", fresh=False)

    op_store.get_suspended_organization = AsyncMock(return_value=SUSPENDED)
    assert not await organizations.account_available(op_store, active)


@pytest.mark.asyncio
async def test_a_store_that_keeps_no_organizations_refuses_nobody() -> None:
    without = object()
    unavailable = MagicMock()
    unavailable.get_suspended_organization = AsyncMock(side_effect=OrganizationsUnavailableError)
    # A double that answers every call with another double has no organization to name.
    double = MagicMock()
    answering = MagicMock()
    answering.get_suspended_organization = AsyncMock(return_value=SUSPENDED)

    assert await organizations.suspended_organization_of(without, "u1") is None
    assert await organizations.suspended_organization_of(unavailable, "u1") is None
    assert await organizations.suspended_organization_of(double, "u1") is None
    assert await organizations.suspended_organization_of(answering, "u1", fresh=True) == SUSPENDED
    answering.get_suspended_organization.assert_awaited_once_with("u1", fresh=True)


# -- a restore, and the batches that wait for it -----------------------------------------


@pytest.mark.asyncio
async def test_a_restore_lifts_the_pause_of_the_members_batches() -> None:
    op_store, batch_store = MagicMock(), MagicMock()
    op_store.list_organization_member_ids = AsyncMock(return_value=["u1", "u2"])
    batch_store.clear_user_batch_pauses = AsyncMock(return_value=3)

    await admin_organizations._continue_batches(op_store, batch_store, "o1")

    batch_store.clear_user_batch_pauses.assert_awaited_once_with(
        ["u1", "u2"], PAUSE_ORGANIZATION_SUSPENDED
    )


@pytest.mark.asyncio
async def test_a_restore_is_done_also_when_the_pause_cannot_be_lifted() -> None:
    op_store, batch_store = MagicMock(), MagicMock()
    op_store.list_organization_member_ids = AsyncMock(return_value=["u1"])
    batch_store.clear_user_batch_pauses = AsyncMock(side_effect=ConnectionError("db blip"))

    # Neither raises: the status is written, and the batches continue when the pause ends.
    await admin_organizations._continue_batches(op_store, batch_store, "o1")
    await admin_organizations._continue_batches(op_store, None, "o1")
