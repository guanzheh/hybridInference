"""A suspended organization over HTTP, against the real store.

The platform administrator suspends an organization and restores it. These
tests show that the gateway then refuses each member wherever it authenticates
an account -- a key, a request on its behalf, a session, a sign-in -- that it
does not refuse a platform administrator, and that a restore brings back what
was there: the same key, the session that was open.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException, Request

from serving import grant_auth, grants, organizations as organization_rules
from serving.config import settings as config
from serving.config.settings import get_settings
from serving.exceptions import KeyOwnerSuspendedError
from serving.servers import auth
from serving.servers.auth import (
    generate_api_key,
    hash_api_key,
    optional_verify_api_key,
    verify_api_key,
    verify_api_key_for_balance,
    verify_batch_management_identity,
)
from serving.servers.routers import agent_grants, internal_lookups
from serving.storage.batch_store import PAUSE_ORGANIZATION_SUSPENDED, BatchStore
from tests.fixtures.oidc_provider import ISSUER
from tests.fixtures.sso_app import (
    EMAIL,
    PASSWORD,
    answer,
    existing_user,
    location,
    new_browser,
    start,
)
from tests.integration import test_organizations_postgres as shared
from tests.integration.test_organizations_postgres import (
    BOB,
    _audit_actions,
    _make_organization,
    _member,
    _organization_with_an_administrator,
    _password_session,
    _platform_admin,
)

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

organization_routes = shared.organization_routes
clean_organizations = shared.clean_organizations
members_sign_straight_in = shared.members_sign_straight_in

CAROL = "carol@example.edu"
REFUSAL = {
    "error": "Organization suspended",
    "code": "organization_suspended",
    "organization": "Example University",
    "contact_email": "",
    "message": (
        "Organization 'Example University' is suspended. "
        "Contact an administrator of your organization."
    ),
}


@pytest.fixture(autouse=True)
def user_auth_on(monkeypatch):
    """The key routes authenticate: with user auth off they admit everyone."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")


async def _key(store, user_id: str) -> str:
    key = generate_api_key()
    await store.create_key(
        key_hash=hash_api_key(key), key_prefix=key[:12], user_id=user_id, account_id=user_id
    )
    return key


async def _suspend(state: dict[str, Any], action: str = "suspend") -> dict[str, Any]:
    root, headers = state["root"]
    response = await root.post(
        f"/admin/organizations/{state['organization_id']}/{action}", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _answer_of(call) -> Any:
    """What a place that authenticates answers: its context, or the refusal it raised."""
    try:
        return await call
    except HTTPException as refused:
        return refused.status_code, refused.detail


def _doors(store, key: str, *, on_behalf_of: str | None = None) -> dict[str, Any]:
    """Each place that takes an API key, called as its route calls it."""
    request, bearer = MagicMock(spec=Request), f"Bearer {key}"
    return {
        # /v1/chat/completions, /v1/messages, /v1/embeddings, /v1/responses ...
        "inference": verify_api_key(
            request=request,
            authorization=bearer,
            x_api_key=None,
            x_on_behalf_of=on_behalf_of,
            op_store=store,
        ),
        # /v1/batches and /v1/files: list, read, cancel, upload. They spend nothing.
        "batches": verify_batch_management_identity(
            request=request,
            authorization=bearer,
            x_api_key=None,
            x_on_behalf_of=None,
            op_store=store,
        ),
        # /v1/models
        "models": optional_verify_api_key(
            request=request, authorization=bearer, x_api_key=None, op_store=store
        ),
        # the balance routes
        "balance": verify_api_key_for_balance(
            request=request, authorization=bearer, x_api_key=None, op_store=store
        ),
    }


async def _through_the_doors(store, key: str) -> dict[str, Any]:
    return {name: await _answer_of(call) for name, call in _doors(store, key).items()}


def _admitted(answers: dict[str, Any], user_id: str) -> bool:
    return all(
        isinstance(answer, dict) and answer["user_id"] == user_id for answer in answers.values()
    )


async def _login_events(pool, user_id: str | None = None, *, email: str | None = None) -> list:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT outcome, failure_reason FROM login_events "
            "WHERE ($1::text IS NOT NULL AND user_id = $1) OR ($2::text IS NOT NULL AND email = $2) "
            "ORDER BY id",
            user_id,
            email,
        )
    return [(row["outcome"], row["failure_reason"]) for row in rows]


async def _sessions(pool, user_id: str) -> list[bool]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT revoked FROM auth_sessions WHERE user_id = $1 ORDER BY created_at", user_id
        )
    return [bool(row["revoked"]) for row in rows]


async def _add_member(pool, organization_id: str, user_id: str) -> None:
    """Make an account a member without a sign-in, for an account that has a password."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO org_memberships (user_id, organization_id) VALUES ($1, $2)",
            user_id,
            organization_id,
        )


# -- a key ---------------------------------------------------------------------------


async def test_a_suspend_refuses_each_key_of_a_member_and_a_restore_brings_the_key_back(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    bob_id = state["bob"][2]
    key = await _key(store, bob_id)
    outsider_id = await existing_user(store, "someone@elsewhere.org")
    outsider_key = await _key(store, outsider_id)
    assert _admitted(await _through_the_doors(store, key), bob_id)

    suspended = await _suspend(state)

    assert suspended["status"] == "suspended"
    # The routes that cost nothing are refused too: a member lists no batch,
    # uploads no file and reads no model list.
    assert await _through_the_doors(store, key) == {
        "inference": (403, REFUSAL),
        "batches": (403, REFUSAL),
        "models": (403, REFUSAL),
        "balance": (403, REFUSAL),
    }
    assert _admitted(await _through_the_doors(store, outsider_key), outsider_id)
    # The key was not touched: the refusal is read, not written.
    async with clean_organizations.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM api_keys WHERE user_id = $1", bob_id) == (
            "active"
        )

    restored = await _suspend(state, "restore")

    assert restored["status"] == "active"
    # The same key, with no other step.
    assert _admitted(await _through_the_doors(store, key), bob_id)
    assert (await _audit_actions(clean_organizations, state["organization_id"]))[-2:] == [
        "organization.suspend",
        "organization.restore",
    ]


async def test_a_suspend_or_a_restore_that_changes_nothing_writes_no_audit_row(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    root, headers = state["root"]

    assert (await _suspend(state, "restore"))["status"] == "active"
    assert (await _suspend(state))["status"] == "suspended"
    assert (await _suspend(state))["status"] == "suspended"
    shown = await root.get(f"/admin/organizations/{state['organization_id']}", headers=headers)
    unknown = await root.post("/admin/organizations/no-such-organization/suspend", headers=headers)

    assert shown.json()["status"] == "suspended"
    assert unknown.status_code == 404
    actions = await _audit_actions(clean_organizations, state["organization_id"])
    assert actions.count("organization.suspend") == 1
    assert actions.count("organization.restore") == 0


async def test_only_the_platform_administrator_suspends_and_restores(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, alice_headers, _ = state["alice"]
    path = f"/admin/organizations/{state['organization_id']}"

    for action in ("suspend", "restore"):
        # An organization administrator, and nobody.
        assert (await alice.post(f"{path}/{action}", headers=alice_headers)).status_code == 403
        assert (await alice.post(f"{path}/{action}")).status_code == 401
    shown = await state["root"][0].get(path, headers=state["root"][1])
    assert shown.json()["status"] == "active"


async def test_a_request_on_behalf_of_a_member_is_refused_and_not_served_as_the_service_account(
    app, idp, store, members_sign_straight_in, monkeypatch
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice_id, bob_id = state["alice"][2], state["bob"][2]
    # Bob has a key, so his quota context answers; Alice has none, so the
    # organization is read on its own. Both must be refused.
    await _key(store, bob_id)
    service_id = await existing_user(store, "assistant@elsewhere.org")
    service_key = await _key(store, service_id)
    outsider_id = await existing_user(store, "someone@elsewhere.org")
    monkeypatch.setattr(
        auth, "_impersonation_service_key_hashes", lambda: frozenset({hash_api_key(service_key)})
    )

    async def on_behalf_of(user_id: str) -> Any:
        doors = _doors(store, service_key, on_behalf_of=user_id)
        for name, call in doors.items():
            if name != "inference":
                call.close()
        return await _answer_of(doors["inference"])

    assert (await on_behalf_of(bob_id))["user_id"] == bob_id
    assert (await on_behalf_of(alice_id))["user_id"] == alice_id

    await _suspend(state)

    assert await on_behalf_of(bob_id) == (403, REFUSAL)
    assert await on_behalf_of(alice_id) == (403, REFUSAL)
    # The service account itself, and an account of no organization, are served.
    assert (await on_behalf_of(outsider_id))["user_id"] == outsider_id
    assert _admitted(await _through_the_doors(store, service_key), service_id)

    await _suspend(state, "restore")

    assert (await on_behalf_of(bob_id))["user_id"] == bob_id
    assert (await on_behalf_of(alice_id))["user_id"] == alice_id


async def test_a_platform_administrator_is_not_refused_and_an_organization_administrator_is(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, alice_headers, alice_id = state["alice"]
    carol, carol_headers, carol_id = await _member(app, idp, store, email=CAROL, sub="subject-3")
    await store.update_user_fields(carol_id, role="admin")
    alice_key, carol_key = await _key(store, alice_id), await _key(store, carol_id)

    await _suspend(state)

    # Alice administers the organization. She is a member, and is refused.
    assert (await _through_the_doors(store, alice_key))["inference"] == (403, REFUSAL)
    refused = await alice.get("/user/organization", headers=alice_headers)
    assert refused.status_code == 403
    assert refused.json()["detail"] == REFUSAL["message"]
    # Carol is a member with the admin role of the platform: nothing refuses
    # her, or nobody could sign in to restore the organization.
    assert _admitted(await _through_the_doors(store, carol_key), carol_id)
    assert (await carol.get("/user/me", headers=carol_headers)).status_code == 200
    assert (await carol.post("/auth/refresh")).status_code == 200
    again = new_browser(app)
    signed_in = await answer(
        again, idp, await start(again, CAROL), sub="subject-3", email=CAROL, name=CAROL
    )
    assert location(signed_in)[1].get("sso") == ["complete"]

    # Without the admin role the suspension applies to her from the next request.
    await store.update_user_fields(carol_id, role="free")

    assert (await _through_the_doors(store, carol_key))["inference"] == (403, REFUSAL)
    assert (await carol.get("/user/me", headers=carol_headers)).status_code == 403


async def test_an_address_of_admin_emails_is_not_refused_while_its_role_is_not_admin_yet(
    app, idp, store, members_sign_straight_in, monkeypatch
):
    """The account gets the role with its next token, so nothing may refuse that token."""
    dave_address = "dave@example.edu"
    state = await _organization_with_an_administrator(app, idp, store)
    dave, dave_headers, dave_id = await _member(
        app, idp, store, email=dave_address, sub="subject-4"
    )
    dave_key = await _key(store, dave_id)
    # Listed after the account exists, and in another case than the account has it.
    monkeypatch.setattr(config.settings, "admin_emails", dave_address.upper())

    await _suspend(state)

    assert (await store.get_user_by_id(dave_id))["role"] == "free"
    assert _admitted(await _through_the_doors(store, dave_key), dave_id)
    assert (await dave.get("/user/me", headers=dave_headers)).status_code == 200
    again = new_browser(app)
    signed_in = await answer(
        again,
        idp,
        await start(again, dave_address),
        sub="subject-4",
        email=dave_address,
        name=dave_address,
    )
    # The callback admits the account by its address: its role is not admin.
    assert location(signed_in)[1].get("sso") == ["complete"]
    # A refresh too. A token gives a listed address the role, so the role is
    # set back first: this refresh passes by the address.
    await store.update_user_fields(dave_id, role="free")
    assert (await dave.post("/auth/refresh")).status_code == 200
    assert (await store.get_user_by_id(dave_id))["role"] == "admin"


async def test_admin_users_has_the_status_of_the_organization_of_an_account(
    app, idp, store, members_sign_straight_in
):
    """The account of a member stays active: its organization says why it is refused."""
    state = await _organization_with_an_administrator(app, idp, store)
    root, headers = state["root"]
    bob_id = state["bob"][2]

    async def shown() -> tuple[str, str, str]:
        listed = await root.get(
            f"/admin/users?organization_id={state['organization_id']}", headers=headers
        )
        detail = await root.get(f"/admin/users/{bob_id}/detail", headers=headers)
        assert (listed.status_code, detail.status_code) == (200, 200), listed.text
        [row] = [user for user in listed.json()["users"] if user["id"] == bob_id]
        return row["status"], row["organization"]["status"], detail.json()["organization"]["status"]

    assert await shown() == ("active", "active", "active")

    await _suspend(state)

    assert await shown() == ("active", "suspended", "suspended")

    await _suspend(state, "restore")

    assert await shown() == ("active", "active", "active")


# -- a session ----------------------------------------------------------------------


async def test_a_session_of_a_member_is_refused_can_sign_out_and_works_again_after_a_restore(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    bob, bob_headers, bob_id = state["bob"]
    carol, carol_headers, carol_id = await _member(app, idp, store, email=CAROL, sub="subject-3")
    assert (await bob.get("/user/me", headers=bob_headers)).status_code == 200

    await _suspend(state)

    for path in ("/user/me", "/user/usage", "/user/api-keys"):
        refused = await bob.get(path, headers=bob_headers)
        assert refused.status_code == 403, path
        assert refused.json()["detail"] == REFUSAL["message"]
    # No session was ended: the refusal is read at each request.
    assert await _sessions(clean_organizations, bob_id) == [False]
    assert await _sessions(clean_organizations, carol_id) == [False]
    # A member can still sign out.
    signed_out = await carol.post("/auth/logout", headers=carol_headers)
    assert signed_out.status_code == 200, signed_out.text
    assert await _sessions(clean_organizations, carol_id) == [True]

    await _suspend(state, "restore")

    # The session that was open works again, with the token it had.
    assert (await bob.get("/user/me", headers=bob_headers)).status_code == 200
    assert (await bob.post("/auth/refresh")).status_code == 200
    # The session that was signed out stays signed out.
    assert (await carol.get("/user/me", headers=carol_headers)).status_code == 401


async def test_refresh_and_switch_give_a_member_no_new_token(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    bob, _bob_headers, bob_id = state["bob"]
    assert (await bob.post("/auth/refresh")).status_code == 200

    await _suspend(state)

    refreshed = await bob.post("/auth/refresh")
    switched = await bob.post("/auth/switch", json={"user_id": bob_id})
    assert (refreshed.status_code, refreshed.json()["detail"]) == (
        401,
        "User account is not active.",
    )
    assert (switched.status_code, switched.json()["detail"]) == (
        401,
        "User account is not active.",
    )

    await _suspend(state, "restore")

    assert (await bob.post("/auth/refresh")).status_code == 200
    assert (await bob.post("/auth/switch", json={"user_id": bob_id})).status_code == 200


# -- a sign-in ----------------------------------------------------------------------


async def test_a_password_signs_no_member_in_whatever_the_status_of_the_organization(
    app, idp, store, clean_organizations
):
    """The password sign-in reads no status of the organization, and needs none.

    Each account that a suspension reaches signs in through single sign-on
    only, so the route refuses it as a wrong password before it reads a status
    (the organization record §4.4). The answer is the same with and without
    the suspension, and no session is made.
    """
    root, headers, _ = await _platform_admin(app, store)
    made = await _make_organization(root, headers)
    organization_id = made["organization"]["id"]
    member_id = await existing_user(store, "dave@example.edu")
    await _add_member(clean_organizations, organization_id, member_id)

    async def sign_in():
        return await new_browser(app).post(
            "/auth/login", json={"email": "dave@example.edu", "password": PASSWORD}
        )

    while_active = await sign_in()
    await root.post(f"/admin/organizations/{organization_id}/suspend", headers=headers)
    while_suspended = await sign_in()

    assert (while_active.status_code, while_suspended.status_code) == (401, 401)
    assert while_active.json() == while_suspended.json()
    assert await _login_events(clean_organizations, member_id) == [
        ("failure", "sso_only"),
        ("failure", "sso_only"),
    ]
    assert await _sessions(clean_organizations, member_id) == []


async def test_single_sign_on_refuses_a_member_as_it_refuses_a_suspended_account(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    _bob, _bob_headers, bob_id = state["bob"]
    sessions_before = await _sessions(clean_organizations, bob_id)

    async def written() -> tuple:
        """What a sign-in writes for the account: on its row and on its identity."""
        async with clean_organizations.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT u.status, u.last_login_at, "
                "(SELECT MAX(i.last_login_at) FROM user_identities i WHERE i.user_id = u.id) "
                "AS identity_last_login_at FROM users u WHERE u.id = $1",
                bob_id,
            )
        return tuple(row)

    before = await written()

    await _suspend(state)

    again = new_browser(app)
    refused = await answer(
        again, idp, await start(again, BOB), sub="subject-2", email=BOB, name=BOB
    )
    # No row of the account is written.
    assert await written() == before
    assert before[0] == "active"
    # The page is told what it is told for a suspended account ...
    assert location(refused)[1].get("sso_error") == ["suspended"]
    # ... and the login event says which of the two it was.
    assert (await _login_events(clean_organizations, email=BOB))[-1] == (
        "failure",
        "sso_organization_suspended",
    )
    assert await _sessions(clean_organizations, bob_id) == sessions_before

    await _suspend(state, "restore")

    _browser, _headers, same_id = await _member(app, idp, store, email=BOB, sub="subject-2")
    assert same_id == bob_id


async def test_a_first_sign_in_through_a_suspended_organization_makes_no_account(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    await _suspend(state)

    browser = new_browser(app)
    refused = await answer(
        browser, idp, await start(browser, CAROL), sub="subject-3", email=CAROL, name=CAROL
    )

    assert location(refused)[1].get("sso_error") == ["suspended"]
    assert await store.get_user_by_email(CAROL) is None
    assert await store.get_user_identity(ISSUER, "subject-3") is None
    assert (await _login_events(clean_organizations, email=CAROL))[-1] == (
        "failure",
        "sso_organization_suspended",
    )
    async with clean_organizations.acquire() as conn:
        members = await conn.fetchval(
            "SELECT COUNT(*) FROM org_memberships WHERE organization_id = $1",
            state["organization_id"],
        )
    assert members == 2

    await _suspend(state, "restore")

    _browser, _headers, carol_id = await _member(app, idp, store, email=CAROL, sub="subject-3")
    membership = await store.get_organization_membership(carol_id)
    assert membership["organization_id"] == state["organization_id"]


async def test_a_link_through_a_suspended_organization_is_refused_on_each_path(
    app, idp, store, clean_organizations, sso, monkeypatch
):
    """The link the callback makes by itself, the one its owner confirms, and the one from settings."""
    root, headers, _ = await _platform_admin(app, store)
    made = await _make_organization(root, headers)
    organization_id = made["organization"]["id"]
    account_id = await existing_user(store)  # alice@example.edu, with a password
    await root.post(f"/admin/organizations/{organization_id}/suspend", headers=headers)

    async def nothing_was_linked() -> None:
        assert await store.get_user_identity(ISSUER, "subject-1") is None
        assert await store.get_organization_membership(account_id) is None

    # 1. The callback links by itself where the provider allows it, and the
    # deployment requires a verified email.
    monkeypatch.setattr(get_settings(), "signup_require_email_verification", True)
    sso.configure(auto_link=True)
    browser = new_browser(app)
    by_itself = await answer(browser, idp, await start(browser))
    assert location(by_itself)[1].get("sso_error") == ["inactive"]
    await nothing_was_linked()

    # 2. The owner of the account confirms a link that waits.
    sso.configure()
    browser = new_browser(app)
    waiting = await answer(browser, idp, await start(browser))
    assert location(waiting)[1].get("sso") == ["link"]
    session = await _password_session(browser, EMAIL)
    confirmed = await browser.post("/auth/sso/link/confirm", headers=session)
    assert confirmed.status_code != 200, confirmed.text
    await nothing_was_linked()

    # 3. The account connects single sign-on from its settings.
    browser = new_browser(app)
    session = await _password_session(browser, EMAIL)
    started = await browser.post(
        "/auth/sso/identities/link", json={"email": EMAIL, "password": PASSWORD}, headers=session
    )
    assert started.status_code == 200, started.text
    params = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    from_settings = await answer(browser, idp, params)
    assert location(from_settings)[1].get("sso_error") == ["inactive"]
    await nothing_was_linked()

    # After a restore the same account links, and becomes a member.
    await root.post(f"/admin/organizations/{organization_id}/restore", headers=headers)
    sso.configure(auto_link=True)
    browser = new_browser(app)
    linked = await answer(browser, idp, await start(browser))
    assert location(linked)[1].get("sso") == ["complete"]
    membership = await store.get_organization_membership(account_id)
    assert membership["organization_id"] == organization_id


# -- an account that another service asks about ----------------------------------------


async def test_the_internal_lookups_and_the_grant_routes_answer_as_for_a_suspended_account(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    bob_id = state["bob"][2]
    outsider_id = await existing_user(store, "someone@elsewhere.org")

    async def answers(user_id: str, *, expect_available: bool = True) -> dict[str, Any]:
        status = await internal_lookups.user_status(user_id, store=store)
        return {
            "status": (status["exists"], status["active"], status["status"]),
            # The catalog and the access of the agent, and the grant routes'
            # check of the account, each read the account by its id.
            "catalog": await _answer_of(
                internal_lookups.model_catalog(
                    user_id, store=store, router_exec=MagicMock(), visibility_resolver=None
                )
            ),
            "grant": await _answer_of(agent_grants._active_user(store, user_id)),
            # The route that mints a grant, called as the control plane calls it.
            "mint": await _answer_of(
                agent_grants.mint_grant(
                    agent_grants.MintGrantRequest(
                        user_id=user_id, external_job_id="job-1", external_attempt_id="1"
                    ),
                    store=store,
                    router_exec=MagicMock(routes={}),
                    visibility_resolver=None,
                )
            )
            if not expect_available
            else None,
            "agent_access": await _answer_of(
                internal_lookups.user_agent_access(user_id, store=store)
            ),
            "available": await organization_rules.account_available(
                store, await store.get_user_by_id(user_id)
            ),
        }

    def refused(answer: Any) -> bool:
        return isinstance(answer, tuple) and answer[0] == 403

    before = await answers(bob_id)
    assert before["status"] == (True, True, "active")
    assert before["available"] and not refused(before["grant"])
    assert not refused(before["agent_access"])

    await _suspend(state)

    during = await answers(bob_id, expect_available=False)
    # "Suspended", so that the caller shows a message and does not archive.
    assert during["status"] == (True, False, "suspended")
    assert refused(during["catalog"]) and refused(during["grant"])
    assert refused(during["mint"]) and refused(during["agent_access"])
    assert during["mint"][1]["error"]["type"] == "subject_unavailable"
    assert not during["available"]
    # Each says what it says for a suspended account, and no more.
    assert during["grant"][1]["error"]["type"] == "subject_unavailable"
    outside = await answers(outsider_id)
    assert outside["status"] == (True, True, "active") and outside["available"]

    await _suspend(state, "restore")

    after = await answers(bob_id)
    assert after["status"] == (True, True, "active")
    assert after["available"] and not refused(after["grant"])


async def test_a_grant_model_call_reads_the_suspension_at_each_call(
    app, idp, store, members_sign_straight_in, monkeypatch
):
    """A grant that was minted before the suspend is refused at its next call."""
    state = await _organization_with_an_administrator(app, idp, store)
    bob_id = state["bob"][2]
    await _key(store, bob_id)  # the quota of the account is stored on its key

    async def a_grant_of_bob(_token: str, *, op_store: Any) -> dict[str, Any]:
        return {
            "grant_id": "agr_1",
            "user_id": bob_id,
            "external_job_id": "job_1",
            "allowed_models": [],
        }

    monkeypatch.setattr(grant_auth, "_resolve_grant", a_grant_of_bob)
    token = f"{grants._TOKEN_PREFIX}." + "x" * 24

    # A grant buys inference and nothing else: the route must be an inference route.
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("203.0.113.9", 4000),
        }
    )

    async def call() -> Any:
        return await _answer_of(
            verify_api_key(
                request=request,
                authorization=f"Bearer {token}",
                x_api_key=None,
                x_on_behalf_of=None,
                op_store=store,
            )
        )

    admitted = await call()
    assert (admitted["user_id"], admitted["agent_grant_id"]) == (bob_id, "agr_1")

    await _suspend(state)

    assert await call() == (403, REFUSAL)

    await _suspend(state, "restore")

    assert (await call())["user_id"] == bob_id


# -- the batches that wait for a restore -------------------------------------------------


async def test_a_restore_lets_the_batches_that_the_suspension_paused_continue(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    bob_id = state["bob"][2]
    outsider_id = await existing_user(store, "someone@elsewhere.org")
    batch_store = BatchStore(clean_organizations)
    app.state.services.batch_store = batch_store
    until = datetime.now(UTC) + timedelta(hours=1)

    async def batch_of(user_id: str, reason: str) -> str:
        model = f"model-{secrets.token_hex(4)}"
        created = await batch_store.create_batch(
            batch_id=f"batch_{secrets.token_hex(12)}",
            user_id=user_id,
            key_hash=None,
            endpoint="/v1/chat/completions",
            model_ids=[model],
            items=[{"custom_id": "one", "model_id": model, "request": {"model": model}}],
            metadata=None,
            completion_window_seconds=3600,
        )
        assert await batch_store.pause_user_batches(user_id, until, reason) >= 1
        return created["id"]

    try:
        await _suspend(state)
        # What the worker does when it finds the owner's organization suspended.
        waiting = await batch_of(bob_id, PAUSE_ORGANIZATION_SUSPENDED)
        # An account of no organization, paused for the same reason by a mistake
        # of this test: a restore of this organization is not its restore.
        elsewhere = await batch_of(outsider_id, PAUSE_ORGANIZATION_SUSPENDED)

        await _suspend(state, "restore")

        row = await batch_store.get_batch(waiting)
        assert (row["paused_until"], row["paused_reason"]) == (None, None)
        assert (await batch_store.get_batch(elsewhere))["paused_until"] == until
        # A restore of an organization that is active lifts what is still paused.
        again = await batch_of(bob_id, PAUSE_ORGANIZATION_SUSPENDED)
        await _suspend(state, "restore")
        assert (await batch_store.get_batch(again))["paused_until"] is None
    finally:
        await batch_store.delete_user_batches(bob_id)
        await batch_store.delete_user_batches(outsider_id)
        app.state.services.batch_store = None


# -- a key for an account that is suspended ----------------------------------------------


async def test_a_key_for_a_suspended_account_is_refused_with_an_answer(
    app, idp, store, clean_organizations, members_sign_straight_in, monkeypatch
):
    """A suspension revokes every key. A key made after it would be live on a closed account."""
    state = await _organization_with_an_administrator(app, idp, store)
    root, headers = state["root"]
    alice, alice_headers, _ = state["alice"]
    bob_id = state["bob"][2]
    suspended = await root.patch(
        f"/admin/users/{bob_id}", json={"status": "suspended"}, headers=headers
    )
    assert suspended.status_code == 200, suspended.text

    # The platform administrator mints a key for the account.
    minted = await root.post(
        "/admin/api-keys", json={"user_id": bob_id, "password": PASSWORD}, headers=headers
    )

    assert minted.status_code == 409, minted.text
    assert "suspended" in minted.json()["detail"]
    async with clean_organizations.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM api_keys WHERE user_id = $1", bob_id) == 0

    # The account itself, when the suspension lands between its session check
    # and the write: a delete of its organization holds the key lock meanwhile.
    refuse = AsyncMock(side_effect=KeyOwnerSuspendedError())
    monkeypatch.setattr(store, "create_key", refuse)
    made = await alice.post("/user/api-keys", json={"name": "after"}, headers=alice_headers)

    assert made.status_code == 409, made.text
    assert "suspended" in made.json()["detail"]
    refuse.assert_awaited_once()
