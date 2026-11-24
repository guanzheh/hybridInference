"""The delete of an organization over HTTP, against the real stores.

A delete writes to two stores: it switches the ``SSO_PROVIDERS`` entries of the
organization off in the settings, then deletes in the operational store. So
these tests run with the real settings store attached and ``SSO_PROVIDERS`` as
a stored row, which the sign-in code under test reads: with the document
replaced by a fixture, a write of the route would never reach a sign-in.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio

from serving.auth import sso_accounts
from serving.config import app_config, sso as sso_config, sso_switch
from serving.config.app_config_store import AppConfigStore, ConfigRow
from serving.servers.auth import generate_api_key, hash_api_key
from serving.servers.routers.admin import organizations as organization_routes_module
from serving.storage import organizations as org_store
from tests.fixtures.oidc_provider import ISSUER
from tests.fixtures.sso_app import (
    PASSWORD,
    Sso,
    existing_user,
    location,
    new_browser,
)
from tests.integration import test_organizations_postgres as shared
from tests.integration.test_organizations_postgres import (
    BOB,
    PLATFORM,
    _audit_actions,
    _bearer,
    _make_organization,
    _member,
    _platform_admin,
    _set_role,
)

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

organization_routes = shared.organization_routes
clean_organizations = shared.clean_organizations

CAROL = "carol@example.edu"
NAME = "Example University"
#: The document the code under test reads in these tests: the ``sso`` fixture
#: replaces this function, and a delete must write what a sign-in then reads.
STORED_DOCUMENT = sso_config.get_sso_document


def _providers() -> list[dict[str, Any]]:
    return [
        Sso.provider(auto_create=True, require_approval=False),
        Sso.provider(
            id="example-staff",
            display_name="Example Staff",
            domains=["staff.example.edu"],
            auto_create=True,
            require_approval=False,
        ),
    ]


@pytest_asyncio.fixture(autouse=True)
async def settings(monkeypatch, sso, auth_backend, clean_organizations):
    """The real settings store, with ``SSO_PROVIDERS`` as a stored row."""
    pool = clean_organizations
    config_store = AppConfigStore(pool)

    async def wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM app_config WHERE key IN ('SSO_PROVIDERS', 'SITE_NAME')")
            await conn.execute("DELETE FROM admin_audit_log WHERE action = 'config.update'")

    await wipe()
    await config_store.write([ConfigRow("SSO_PROVIDERS", json.dumps({"providers": _providers()}))])
    # Before the load: it tells the listeners, and theirs clears this cache.
    monkeypatch.setattr(sso_config, "get_sso_document", STORED_DOCUMENT)
    await app_config.load(config_store, bootstrap=False)
    app_config.attach(config_store)
    STORED_DOCUMENT.cache_clear()
    try:
        yield config_store
    finally:
        app_config.reset_state()
        STORED_DOCUMENT.cache_clear()
        await wipe()


# -- helpers -------------------------------------------------------------------------


async def _delete(browser, headers, organization_id: str, **body: Any):
    payload = {"confirm_name": NAME, "password": PASSWORD, **body}
    return await browser.request(
        "DELETE", f"/admin/organizations/{organization_id}", json=payload, headers=headers
    )


def _code(response) -> tuple[int, str | None]:
    detail = response.json().get("detail")
    code = detail["error"]["code"] if isinstance(detail, dict) and "error" in detail else None
    return response.status_code, code


async def _key(store, user_id: str) -> str:
    key = generate_api_key()
    await store.create_key(
        key_hash=hash_api_key(key), key_prefix=key[:12], user_id=user_id, account_id=user_id
    )
    return key


async def _entries(config_store) -> dict[str, bool]:
    """Whether each entry of the stored ``SSO_PROVIDERS`` row is switched on."""
    rows = (await config_store.fetch_snapshot()).rows
    document = json.loads(rows["SSO_PROVIDERS"].value)
    return {entry["id"]: entry.get("enabled", True) for entry in document["providers"]}


async def _config_audit(pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT organization_id, actor_type, actor_id, target_type, target_id, "
            "details::text AS details FROM admin_audit_log WHERE action = 'config.update' "
            "ORDER BY id"
        )
    return [dict(row) for row in rows]


async def _accounts(pool, *user_ids: str) -> dict[str, tuple]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT u.id, u.status, u.suspension_message, "
            "(SELECT array_agg(k.status ORDER BY k.id) FROM api_keys k WHERE k.user_id = u.id) "
            "AS keys, "
            "(SELECT array_agg(s.revoked ORDER BY s.created_at) FROM auth_sessions s "
            " WHERE s.user_id = u.id) AS sessions "
            "FROM users u WHERE u.id = ANY($1::text[])",
            list(user_ids),
        )
    return {
        row["id"]: (row["status"], row["suspension_message"], row["keys"], row["sessions"])
        for row in rows
    }


async def _organization(app, idp, store) -> dict[str, Any]:
    """Alice administers it, Bob is a member with a key, Carol is a member and a platform admin."""
    root, root_headers, root_id = await _platform_admin(app, store)
    alice, alice_headers, alice_id = await _member(app, idp, store)
    bob, bob_headers, bob_id = await _member(app, idp, store, email=BOB, sub="subject-2")
    carol, _stale, carol_id = await _member(app, idp, store, email=CAROL, sub="subject-3")
    await store.update_user_fields(carol_id, role="admin")
    made = await _make_organization(root, root_headers)
    organization_id = made["organization"]["id"]
    await _set_role(root, root_headers, organization_id, alice_id)
    await _key(store, bob_id)
    await _key(store, carol_id)
    return {
        "organization_id": organization_id,
        "root": (root, root_headers, root_id),
        "alice": (alice, alice_headers, alice_id),
        "bob": (bob, bob_headers, bob_id),
        "carol": (carol, await _bearer(carol), carol_id),
    }


async def _exists(state: dict[str, Any]) -> bool:
    root, headers, _ = state["root"]
    shown = await root.get(f"/admin/organizations/{state['organization_id']}", headers=headers)
    return shown.status_code == 200


# -- what a delete does ---------------------------------------------------------------


async def test_a_delete_suspends_the_members_removes_the_organization_and_switches_its_entries_off(
    app, idp, store, clean_organizations, settings
):
    state = await _organization(app, idp, store)
    root, headers, root_id = state["root"]
    organization_id = state["organization_id"]
    alice_id, bob_id, carol_id = state["alice"][2], state["bob"][2], state["carol"][2]
    pending_id = await existing_user(store, "erin@example.edu", status="pending_approval")
    async with clean_organizations.acquire() as conn:
        await conn.execute(
            "INSERT INTO org_memberships (user_id, organization_id) VALUES ($1, $2)",
            pending_id,
            organization_id,
        )
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd) VALUES ($1, '2026-11-20', 3)",
            bob_id,
        )
        await conn.execute(
            "INSERT INTO spend_counters (scope_id, period, cost_usd) VALUES ($1, 'm:2026-11', 3)",
            org_store.organization_scope(organization_id),
        )
    before = await _accounts(clean_organizations, carol_id, pending_id, root_id)
    async with clean_organizations.acquire() as conn:
        projects_before = await conn.fetch(
            "SELECT id, user_id, status FROM projects WHERE user_id = ANY($1::text[]) ORDER BY id",
            [alice_id, bob_id],
        )
    assert projects_before, "a member has a project before the delete"

    # No ``disable_connections`` in the body: switching the entries off is the default.
    deleted = await _delete(root, headers, organization_id)

    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "message": "The organization Example University was deleted.",
        "accounts_suspended": 2,
        "connections_disabled": ["example-univ"],
    }
    assert not await _exists(state)
    async with clean_organizations.acquire() as conn:
        left = await conn.fetchrow(
            "SELECT (SELECT COUNT(*) FROM org_memberships) AS members, "
            "(SELECT COUNT(*) FROM org_sso_connections) AS connections, "
            "(SELECT COUNT(*) FROM spend_counters WHERE scope_id LIKE 'o:%') AS counters, "
            "(SELECT COUNT(*) FROM user_daily_cost WHERE user_id = $1) AS usage",
            bob_id,
        )
    assert dict(left) == {"members": 0, "connections": 0, "counters": 0, "usage": 1}
    # Admin Users shows the accounts in no organization.
    in_none = await root.get("/admin/users?organization_id=none", headers=headers)
    shown = {user["id"]: user["organization"] for user in in_none.json()["users"]}
    assert (shown[alice_id], shown[bob_id]) == (None, None)
    detail = await root.get(f"/admin/users/{bob_id}/detail", headers=headers)
    assert detail.json()["organization"] is None
    # The list offers the connection of the deleted organization again, as switched off.
    offered = (await root.get("/admin/organizations", headers=headers)).json()
    assert {
        entry["provider_id"]: entry["enabled"] for entry in offered["available_connections"]
    } == {"example-univ": False, "example-staff": True}
    # The projects of the members stay as they were.
    async with clean_organizations.acquire() as conn:
        projects_after = await conn.fetch(
            "SELECT id, user_id, status FROM projects WHERE user_id = ANY($1::text[]) ORDER BY id",
            [alice_id, bob_id],
        )
    assert projects_after == projects_before
    # The member and the organization administrator: suspended, with the name
    # of the organization in the message, their keys revoked, their sessions ended.
    message = "The organization Example University was deleted."
    after = await _accounts(clean_organizations, alice_id, bob_id)
    assert after[alice_id] == ("suspended", message, None, [True])
    assert after[bob_id] == ("suspended", message, ["revoked"], [True])
    # A platform administrator, an account that waits for approval and the
    # caller: as they were.
    assert await _accounts(clean_organizations, carol_id, pending_id, root_id) == before
    assert before[carol_id][0] == "active" and before[pending_id][0] == "pending_approval"

    # The entry of its connection is off in the stored document ...
    assert await _entries(settings) == {"example-univ": False, "example-staff": True}
    # ... so nobody starts a sign-in through it, and no account is made.
    discovered = await new_browser(app).post(
        "/auth/sso/discover", json={"email": "new@example.edu"}
    )
    assert discovered.json()["available"] is False
    started = await new_browser(app).get("/auth/sso/example-univ/start")
    assert location(started)[1].get("sso_error") == ["unavailable"]
    assert await store.get_user_by_email("new@example.edu") is None

    # The change of the setting is recorded as a save in Admin Settings is, and
    # belongs to no organization.
    [config_row] = await _config_audit(clean_organizations)
    assert config_row["organization_id"] is None
    assert (config_row["actor_type"], config_row["actor_id"]) == ("admin", root_id)
    assert (config_row["target_type"], config_row["target_id"]) == ("config", "SSO_PROVIDERS")
    # The rows of the organization stay, and the filter still reads them.
    assert (await _audit_actions(clean_organizations, organization_id))[-1] == (
        "organization.delete"
    )
    async with clean_organizations.acquire() as conn:
        details = await conn.fetchval(
            "SELECT details::text FROM admin_audit_log WHERE action = 'organization.delete'"
        )
    assert json.loads(details) == {
        "name": NAME,
        "members": 4,
        "accounts_suspended": 2,
        "connections": ["example-univ"],
        "connections_disabled": ["example-univ"],
    }
    listed = await root.get(
        f"/admin/audit-log?organization_id={organization_id}&limit=50", headers=headers
    )
    assert listed.status_code == 200, listed.text
    # The filter reads the rows of an organization that is gone, the delete too.
    assert "organization.delete" in listed.text and "organization.create" in listed.text
    assert "config.update" not in listed.text


async def test_a_delete_is_refused_in_this_order_and_changes_nothing(
    app, idp, store, clean_organizations, settings, monkeypatch
):
    from serving.config.settings import get_settings

    monkeypatch.setenv("ADMIN_TOKEN", "org-itest-admin-token")
    get_settings.cache_clear()
    token = {"Authorization": "Bearer org-itest-admin-token"}
    state = await _organization(app, idp, store)
    root, headers, _ = state["root"]
    carol, carol_headers, _ = state["carol"]
    organization_id = state["organization_id"]
    version = app_config.source_version()
    accounts = [state[name][2] for name in ("alice", "bob", "carol")]
    before = await _accounts(clean_organizations, *accounts)

    unknown = await _delete(root, headers, "no-such-organization")
    # A wrong name is refused before the password is looked at: the wrong
    # password here would be a 400, and would count as a sign-in attempt.
    wrong_name = await _delete(
        root, headers, organization_id, confirm_name="example university", password="wrong"
    )
    # Carol is a platform administrator who signs in only through the
    # connection that the delete would switch off.
    locked_out = await _delete(carol, carol_headers, organization_id, password=None)
    unconfirmed = await _delete(root, headers, organization_id, password=None)
    wrong_password = await _delete(root, headers, organization_id, password="not-the-password")
    by_token = await _delete(root, token, organization_id)

    assert unknown.status_code == 404
    assert _code(wrong_name) == (409, "confirm_name_mismatch")
    assert _code(locked_out) == (409, "last_sign_in")
    assert (unconfirmed.status_code, wrong_password.status_code) == (400, 400)
    assert by_token.status_code == 403
    # Nothing changed: no row, and no setting.
    assert await _exists(state)
    assert await _accounts(clean_organizations, *accounts) == before
    assert await _entries(settings) == {"example-univ": True, "example-staff": True}
    assert app_config.source_version() == version
    assert await _config_audit(clean_organizations) == []
    assert "organization.delete" not in await _audit_actions(clean_organizations, organization_id)


async def test_a_delete_is_refused_while_the_stored_document_does_not_validate(
    app, idp, store, clean_organizations, settings
):
    """Read as "no providers", it would let the delete turn nothing off."""
    state = await _organization(app, idp, store)
    root, headers, _ = state["root"]
    broken = _providers()
    broken[1]["domains"] = ["example.edu"]  # two providers claim one domain
    await settings.write([ConfigRow("SSO_PROVIDERS", json.dumps({"providers": broken}))])

    wrong_name = await _delete(
        root, headers, state["organization_id"], confirm_name="example university"
    )
    refused = await _delete(root, headers, state["organization_id"], password="wrong")
    kept_on = await _delete(root, headers, state["organization_id"], disable_connections=False)

    # The name is checked before the settings are read.
    assert _code(wrong_name) == (409, "confirm_name_mismatch")
    # Before the confirmation: the wrong password was not looked at.
    assert _code(refused) == (409, "settings_invalid")
    # The administrator can still delete, and keep the connections as they are.
    assert kept_on.status_code == 200, kept_on.text
    assert kept_on.json()["connections_disabled"] == []


async def test_a_caller_with_another_way_to_sign_in_is_not_refused(
    app, idp, store, clean_organizations, settings
):
    state = await _organization(app, idp, store)
    _carol, _headers, carol_id = state["carol"]
    carol = await store.get_user_by_id(carol_id)
    root_account = await store.get_user_by_id(state["root"][2])

    # Carol has only the identity of the connection; the caller of the other
    # tests has a password.
    assert await sso_accounts.loses_sign_in(store, carol, ["example-univ"])
    assert not await sso_accounts.loses_sign_in(store, root_account, ["example-univ"])
    # An entry that is not hers does not concern her, and neither does none.
    assert not await sso_accounts.loses_sign_in(store, carol, ["example-staff"])
    assert not await sso_accounts.loses_sign_in(store, carol, [])
    # With an identity that an entry which stays on serves, she has another
    # way in: switching her organization's entry off does not lock her out.
    staff = _providers()
    staff[1]["issuer"] = "https://staff-idp.example.edu/realms/main"
    await settings.write([ConfigRow("SSO_PROVIDERS", json.dumps({"providers": staff}))])
    await app_config.refresh()
    async with clean_organizations.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_identities (id, user_id, provider_id, issuer, subject, email) "
            "VALUES ('i-carol-staff', $1, 'example-staff', $2, 'staff-carol', $3)",
            carol_id,
            staff[1]["issuer"],
            "carol@staff.example.edu",
        )
    assert not await sso_accounts.loses_sign_in(store, carol, ["example-univ"])
    # Not when that entry is switched off with it.
    assert await sso_accounts.loses_sign_in(store, carol, ["example-univ", "example-staff"])
    # Nor when it was switched off before: an identity whose entry is off is no way in.
    staff[1]["enabled"] = False
    await settings.write([ConfigRow("SSO_PROVIDERS", json.dumps({"providers": staff}))])
    await app_config.refresh()
    assert await sso_accounts.loses_sign_in(store, carol, ["example-univ"])
    # With a verified phone she has another way in.
    async with clean_organizations.acquire() as conn:
        await conn.execute(
            "UPDATE users SET phone = '+15550100', phone_verified = TRUE WHERE id = $1", carol_id
        )
    carol = await store._store.get_user_by_id(carol_id)
    assert not await sso_accounts.loses_sign_in(store, carol, ["example-univ"])


async def test_a_delete_can_keep_the_connections_on(app, idp, store, clean_organizations, settings):
    state = await _organization(app, idp, store)
    root, headers, _ = state["root"]
    version = app_config.source_version()

    # Carol signs in only through the connection. With the connections kept on
    # the check of the caller is skipped: she is not told `last_sign_in`, and
    # gets as far as the confirmation, which she has not given.
    carol, carol_headers, _ = state["carol"]
    unconfirmed = await _delete(
        carol, carol_headers, state["organization_id"], password=None, disable_connections=False
    )
    assert _code(unconfirmed) == (400, None)

    deleted = await _delete(root, headers, state["organization_id"], disable_connections=False)

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["connections_disabled"] == []
    assert await _entries(settings) == {"example-univ": True, "example-staff": True}
    assert app_config.source_version() == version
    assert await _config_audit(clean_organizations) == []
    # A first sign-in through the entry, which has auto_create, makes a
    # personal account, as it did before there were organizations.
    _browser, _headers, dave_id = await _member(
        app, idp, store, email="dave@example.edu", sub="subject-4"
    )
    assert await store.get_organization_membership(dave_id) is None


async def test_an_entry_that_was_off_needs_no_write(app, idp, store, clean_organizations, settings):
    state = await _organization(app, idp, store)
    root, headers, root_id = state["root"]
    version = await sso_switch.read_version()
    switched, _changes = await sso_switch.switch_off(
        ["example-univ"], version=version, updated_by=root_id
    )
    assert switched == ["example-univ"]
    version = app_config.source_version()

    deleted = await _delete(root, headers, state["organization_id"])

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["connections_disabled"] == []
    assert app_config.source_version() == version
    assert await _config_audit(clean_organizations) == []


# -- what can come between ---------------------------------------------------------------


async def test_a_failure_inside_the_delete_leaves_the_organization_with_its_entries_off(
    app, idp, store, clean_organizations, settings, monkeypatch
):
    state = await _organization(app, idp, store)
    root, headers, _ = state["root"]
    organization_id = state["organization_id"]
    accounts = [state[name][2] for name in ("alice", "bob", "carol")]
    before = await _accounts(clean_organizations, *accounts)
    write_audit_row = org_store.insert_audit_row

    async def fail_on_the_delete(conn, **row):
        if row["action"] == "organization.delete":
            raise RuntimeError("the audit row could not be written")
        return await write_audit_row(conn, **row)

    monkeypatch.setattr(org_store, "insert_audit_row", fail_on_the_delete)

    failed = await _delete(root, headers, organization_id)

    assert _code(failed) == (500, "delete_incomplete")
    assert "switched off" in failed.json()["detail"]["error"]["message"]
    # Every row is as it was; the entry is off, as the answer says.
    assert await _exists(state)
    assert await _accounts(clean_organizations, *accounts) == before
    assert await _entries(settings) == {"example-univ": False, "example-staff": True}
    assert len(await _config_audit(clean_organizations)) == 1

    monkeypatch.setattr(org_store, "insert_audit_row", write_audit_row)
    version = app_config.source_version()
    again = await _delete(root, headers, organization_id)

    # The second delete completes, and writes no setting.
    assert again.status_code == 200, again.text
    assert again.json()["connections_disabled"] == []
    assert app_config.source_version() == version
    assert len(await _config_audit(clean_organizations)) == 1
    assert not await _exists(state)


async def _between_the_checks_and_the_writes(monkeypatch, happens):
    """Run *happens* after the route confirmed the administrator and before it writes.

    Returns the function that puts the route back as it was.
    """
    confirm = organization_routes_module.confirm_administrator

    async def confirm_then(*args: Any, **kwargs: Any) -> None:
        await confirm(*args, **kwargs)
        await happens()

    monkeypatch.setattr(organization_routes_module, "confirm_administrator", confirm_then)
    return lambda: monkeypatch.setattr(organization_routes_module, "confirm_administrator", confirm)


async def test_a_save_of_the_settings_in_between_stops_the_delete(
    app, idp, store, clean_organizations, settings, monkeypatch
):
    state = await _organization(app, idp, store)
    root, headers, _ = state["root"]

    async def another_administrator_saves() -> None:
        await app_config.update({"SITE_NAME": "Saved Meanwhile"}, updated_by="another")

    await _between_the_checks_and_the_writes(monkeypatch, another_administrator_saves)

    refused = await _delete(root, headers, state["organization_id"])

    assert _code(refused) == (409, "settings_changed")
    # Nothing was deleted, nothing was switched off, and the other save is not lost.
    assert await _exists(state)
    assert await _entries(settings) == {"example-univ": True, "example-staff": True}
    assert app_config.config_value("SITE_NAME") == "Saved Meanwhile"
    assert await _config_audit(clean_organizations) == []


@pytest.mark.parametrize("change", ["attach", "rename"])
async def test_a_change_of_the_organization_in_between_stops_the_delete(
    app, idp, store, clean_organizations, settings, monkeypatch, change
):
    state = await _organization(app, idp, store)
    root, headers, _ = state["root"]
    organization_id = state["organization_id"]
    accounts = [state[name][2] for name in ("alice", "bob", "carol")]
    before = await _accounts(clean_organizations, *accounts)

    async def the_organization_changes() -> None:
        if change == "attach":
            outcome, _added = await store.attach_organization_connection(
                organization_id, "example-staff", accounts_of=lambda _rows: [], actor=PLATFORM
            )
        else:
            outcome = await store.rename_organization(
                organization_id, "Example College", actor=PLATFORM
            )
        assert outcome == org_store.UPDATED

    put_back = await _between_the_checks_and_the_writes(monkeypatch, the_organization_changes)

    refused = await _delete(root, headers, organization_id)

    assert _code(refused) == (409, "organization_changed")
    assert "Nothing was deleted" in refused.json()["detail"]["error"]["message"]
    assert await _exists(state)
    assert await _accounts(clean_organizations, *accounts) == before
    # The entry that the route handled is off, and the answer says so.
    assert "switched off" in refused.json()["detail"]["error"]["message"]
    assert await _entries(settings) == {"example-univ": False, "example-staff": True}

    put_back()
    name = "Example College" if change == "rename" else NAME
    again = await _delete(root, headers, organization_id, confirm_name=name)

    # A second delete, with what the organization is now, completes: and it
    # switches off the entry of the connection that came in between.
    assert again.status_code == 200, again.text
    assert again.json()["connections_disabled"] == (["example-staff"] if change == "attach" else [])
    assert (await _entries(settings))["example-staff"] is (change != "attach")


async def test_the_second_of_two_deletes_gets_a_404(
    app, idp, store, clean_organizations, settings, monkeypatch
):
    state = await _organization(app, idp, store)
    root, headers, _ = state["root"]
    organization_id = state["organization_id"]

    async def another_delete_finishes_first() -> None:
        outcome, _result = await store.delete_organization(
            organization_id, confirm_name=NAME, actor=PLATFORM
        )
        assert outcome == org_store.DELETED

    await _between_the_checks_and_the_writes(monkeypatch, another_delete_finishes_first)

    second = await _delete(root, headers, organization_id)

    assert second.status_code == 404, second.text
    actions = await _audit_actions(clean_organizations, organization_id)
    assert actions.count("organization.delete") == 1
    # The first delete here is a call of the store, which switches nothing
    # off. The second one asked for the entries to be off and switched them
    # off before it found the organization gone: they stay off.
    assert await _entries(settings) == {"example-univ": False, "example-staff": True}


async def test_a_failure_while_the_entries_are_switched_off_is_not_called_a_clean_refusal(
    app, idp, store, clean_organizations, settings, monkeypatch
):
    """The settings can have stored the change before they failed: the answer says so."""
    state = await _organization(app, idp, store)
    root, headers, _ = state["root"]
    switch_off = sso_switch.switch_off

    async def store_then_fail(*args: Any, **kwargs: Any):
        await switch_off(*args, **kwargs)
        raise ConnectionError("the reload after the commit failed")

    monkeypatch.setattr(sso_switch, "switch_off", store_then_fail)

    failed = await _delete(root, headers, state["organization_id"])

    assert _code(failed) == (500, "delete_incomplete")
    assert "may be switched off" in failed.json()["detail"]["error"]["message"]
    assert await _exists(state)
    assert await _entries(settings) == {"example-univ": False, "example-staff": True}

    monkeypatch.setattr(sso_switch, "switch_off", switch_off)
    assert (await _delete(root, headers, state["organization_id"])).status_code == 200


# -- after a delete ---------------------------------------------------------------------


async def test_after_a_delete_an_account_is_in_no_organization_until_its_connection_is_attached(
    app, idp, store, clean_organizations, settings
):
    state = await _organization(app, idp, store)
    root, headers, _ = state["root"]
    bob_id = state["bob"][2]
    assert (await _delete(root, headers, state["organization_id"])).status_code == 200

    # The platform administrator makes the account active again, in Admin → Users.
    resumed = await root.patch(f"/admin/users/{bob_id}", json={"status": "active"}, headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert (await store.get_user_by_id(bob_id))["status"] == "active"
    assert await store.get_organization_membership(bob_id) is None
    async with clean_organizations.acquire() as conn:
        keys = await conn.fetch("SELECT status FROM api_keys WHERE user_id = $1", bob_id)
    assert [row["status"] for row in keys] == ["revoked"]

    # The connection, whose entry is off, is attached to a new organization:
    # the accounts that match the member rule become members, the suspended too.
    made = await _make_organization(root, headers, name="Example Reborn")

    assert made["members_added"] == 3
    alice = await store.get_user_by_id(state["alice"][2])
    membership = await store.get_organization_membership(alice["id"])
    assert membership["organization_id"] == made["organization"]["id"]
    assert alice["status"] == "suspended"
    assert [c["enabled"] for c in made["organization"]["connections"]] == [False]
    assert await store.get_user_identity(ISSUER, "subject-1") is not None
    # Nobody signs in through it until the entry is switched on again.
    refused = await new_browser(app).post("/auth/sso/discover", json={"email": BOB})
    assert refused.json()["available"] is False
