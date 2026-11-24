"""The status of an organization and its delete, in the store, against real PostgreSQL.

The rows are tagged as in ``test_organizations_store_postgres`` (``o-itest-*``
organizations, ``u-org-*`` accounts, ``itest-*`` providers), whose fixture and
helpers these tests use.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta

import pytest

from serving import organizations as account_rules
from serving.config import settings as config
from serving.exceptions import KeyOwnerSuspendedError
from serving.storage import organizations as orgs
from serving.storage.cache import CachedOperationalStore, InMemoryCache
from tests.integration.storage import test_organizations_store_postgres as shared
from tests.integration.storage.test_organizations_store_postgres import (
    PLATFORM,
    _account,
    _audit,
    _key,
    _key_hash,
    _organization,
    _sign_in,
    _waits_for_the_other,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

store = shared.store


async def _session(op_store, user_id: str) -> str:
    sid = secrets.token_hex(8)
    await op_store.create_session(
        session_id=f"s-{secrets.token_hex(6)}",
        user_id=user_id,
        refresh_token_hash=secrets.token_hex(16),
        jti=secrets.token_hex(8),
        sid=sid,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return sid


async def _member_role(pool, user_id: str, role: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("UPDATE org_memberships SET role = $2 WHERE user_id = $1", user_id, role)


async def _state(pool, *user_ids: str) -> dict:
    """Every row a suspension could have touched, for the accounts given."""
    ids = list(user_ids)
    async with pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT id, status, suspension_message, role FROM users "
            "WHERE id = ANY($1::text[]) ORDER BY id",
            ids,
        )
        keys = await conn.fetch(
            "SELECT id, user_id, status FROM api_keys WHERE user_id = ANY($1::text[]) ORDER BY id",
            ids,
        )
        sessions = await conn.fetch(
            "SELECT id, user_id, revoked FROM auth_sessions "
            "WHERE user_id = ANY($1::text[]) ORDER BY id",
            ids,
        )
        members = await conn.fetch(
            "SELECT user_id, organization_id, role, removed_at FROM org_memberships "
            "WHERE user_id = ANY($1::text[]) ORDER BY user_id",
            ids,
        )
    return {
        "users": [dict(row) for row in users],
        "keys": [dict(row) for row in keys],
        "sessions": [dict(row) for row in sessions],
        "members": [dict(row) for row in members],
    }


async def _organization_rows(pool, organization_id: str) -> dict:
    async with pool.acquire() as conn:
        return {
            "organizations": await conn.fetchval(
                "SELECT COUNT(*) FROM orgs WHERE id = $1", organization_id
            ),
            "members": await conn.fetchval(
                "SELECT COUNT(*) FROM org_memberships WHERE organization_id = $1",
                organization_id,
            ),
            "connections": await conn.fetchval(
                "SELECT COUNT(*) FROM org_sso_connections WHERE organization_id = $1",
                organization_id,
            ),
            "counters": await conn.fetchval(
                "SELECT COUNT(*) FROM spend_counters WHERE scope_id = $1",
                orgs.organization_scope(organization_id),
            ),
        }


# -- suspend and restore ------------------------------------------------------


async def test_a_suspend_writes_the_status_and_its_audit_row_and_nothing_else(store):
    op_store, pool = store
    member = await _account(op_store, "sa1")
    await _key(op_store, member)
    await _session(op_store, member)
    await _organization(op_store, "sa", ("itest-sa", [member]))
    before = await _state(pool, member)

    suspended = await op_store.set_organization_status("o-itest-sa", "suspended", actor=PLATFORM)

    assert suspended == orgs.UPDATED
    assert (await op_store.get_organization("o-itest-sa"))["status"] == "suspended"
    # The account, its key, its session and its membership are as they were.
    assert await _state(pool, member) == before
    assert before["keys"][0]["status"] == "active"
    assert before["sessions"][0]["revoked"] is False

    again = await op_store.set_organization_status("o-itest-sa", "suspended", actor=PLATFORM)
    restored = await op_store.set_organization_status("o-itest-sa", "active", actor=PLATFORM)

    assert (again, restored) == (orgs.UNCHANGED, orgs.UPDATED)
    assert (await op_store.get_organization("o-itest-sa"))["status"] == "active"
    assert await _state(pool, member) == before
    rows = await _audit(pool, "o-itest-sa")
    # The second suspend changed nothing, so it wrote no row.
    assert [row["action"] for row in rows] == [
        "organization.create",
        "organization.suspend",
        "organization.restore",
    ]
    assert (rows[1]["actor_type"], rows[1]["actor_id"]) == ("admin", "u-org-root")
    assert (rows[1]["target_type"], rows[1]["target_id"]) == ("organization", "o-itest-sa")
    assert json.loads(rows[1]["details"]) == {"status": {"old": "active", "new": "suspended"}}


async def test_a_status_is_one_of_two_and_an_unknown_organization_is_not_found(store):
    op_store, _pool = store

    assert (
        await op_store.set_organization_status("o-itest-nobody", "suspended", actor=PLATFORM)
        == orgs.NOT_FOUND
    )
    with pytest.raises(ValueError, match="unknown organization status"):
        await op_store.set_organization_status("o-itest-nobody", "deleted", actor=PLATFORM)


async def test_a_new_organization_is_active(store):
    op_store, _pool = store
    await _organization(op_store, "sb")

    assert (await op_store.get_organization("o-itest-sb"))["status"] == "active"
    [listed] = [row for row in await op_store.list_organizations() if row["id"] == "o-itest-sb"]
    assert listed["status"] == "active"


async def test_every_lookup_says_for_whom_the_organization_is_suspended(store, monkeypatch):
    """A member and an organization administrator: yes. A platform administrator: no.

    A platform administrator by its role, or by an ``ADMIN_EMAILS`` address
    while its role does not say so yet.
    """
    op_store, pool = store
    member = await _account(op_store, "sc1")
    organization_admin = await _account(op_store, "sc2")
    platform_admin = await _account(op_store, "sc3", role="admin")
    outsider = await _account(op_store, "sc4")
    listed_admin = await _account(op_store, "sc5")
    # Written in another case than the account has it: the comparison ignores case.
    monkeypatch.setattr(config.settings, "admin_emails", "SC5@org-itest.example.edu")
    accounts = (member, organization_admin, platform_admin, outsider, listed_admin)
    hashes = {user_id: await _key_hash(pool, await _key(op_store, user_id)) for user_id in accounts}
    await _organization(
        op_store, "sc", ("itest-sc", [member, organization_admin, platform_admin, listed_admin])
    )
    await _member_role(pool, organization_admin, "admin")

    async def answers() -> dict[str, list]:
        found = {}
        for user_id in accounts:
            by_key = await op_store.get_auth_context_by_key_hash(hashes[user_id])
            with_spend = await op_store.get_auth_context_with_spend(hashes[user_id])
            light = await op_store.get_auth_context_lightweight(hashes[user_id])
            audit = await op_store.get_key_owner_for_audit(hashes[user_id])
            [by_account] = await op_store.get_quota_context_for_user(user_id)
            reader = await op_store.get_suspended_organization(user_id)
            found[user_id] = [
                by_key["organization_suspended"],
                with_spend["organization_suspended"],
                light["organization_suspended"],
                audit["organization_suspended"],
                by_account["organization_suspended"],
                reader is not None,
            ]
        return found

    assert await answers() == {user_id: [False] * 6 for user_id in accounts}

    await op_store.set_organization_status("o-itest-sc", "suspended", actor=PLATFORM)

    refused = await answers()
    assert refused == {
        member: [True] * 6,
        organization_admin: [True] * 6,
        platform_admin: [False] * 6,
        outsider: [False] * 6,
        listed_admin: [False] * 6,
    }
    # Who a suspension reaches is who signs in through single sign-on only. So
    # the password and the phone sign-in, which refuse those accounts, need no
    # check of the organization.
    async with pool.acquire() as conn:
        sso_only = {
            user_id: await orgs.signs_in_through_sso_only(conn, user_id) for user_id in accounts
        }
    assert sso_only == {user_id: answer[-1] for user_id, answer in refused.items()}
    # The two sign-in routes decide with the same rule written in Python.
    by_the_routes = {
        user_id: await account_rules.signs_in_through_sso_only(
            op_store, await op_store.get_user_by_id(user_id)
        )
        for user_id in accounts
    }
    assert by_the_routes == sso_only
    # A sign-in that would make a member of the suspended organization: refused
    # for an account that the suspension reaches, and for no other.
    async with pool.acquire() as conn:
        would_refuse = {
            name: await orgs.refused_by_connection(
                conn, provider_id="itest-sc", account_role=role, account_email=email
            )
            for name, role, email in (
                ("a new person", None, "new@org-itest.example.edu"),
                ("a platform administrator", "admin", "root@org-itest.example.edu"),
                ("a listed address", "free", "sc5@org-itest.example.edu"),
            )
        }
    assert would_refuse == {
        "a new person": True,
        "a platform administrator": False,
        "a listed address": False,
    }
    assert await op_store.get_suspended_organization(member) == {
        "organization_id": "o-itest-sc",
        "organization_name": "Itest sc",
    }
    # The lookup that reads no limit still names the organization a refusal gives.
    light = await op_store.get_auth_context_lightweight(hashes[member])
    assert (light["organization_id"], light["organization_name"]) == ("o-itest-sc", "Itest sc")

    await op_store.set_organization_status("o-itest-sc", "active", actor=PLATFORM)

    assert await answers() == {user_id: [False] * 6 for user_id in accounts}


async def test_a_cached_answer_does_not_outlive_a_suspend_a_restore_or_a_change_of_role(store):
    raw_store, pool = store
    op_store = CachedOperationalStore(raw_store, InMemoryCache())
    member = await _account(op_store, "sd1")
    key_hash = await _key_hash(pool, await _key(op_store, member))
    await _organization(op_store, "sd", ("itest-sd", [member]))

    async def cached() -> tuple[bool, bool, bool]:
        by_key = await op_store.get_auth_context_by_key_hash(key_hash)
        light = await op_store.get_auth_context_lightweight(key_hash)
        by_account = await op_store.get_suspended_organization(member)
        return (
            by_key["organization_suspended"],
            light["organization_suspended"],
            by_account is not None,
        )

    assert await cached() == (False, False, False)

    await op_store.set_organization_status("o-itest-sd", "suspended", actor=PLATFORM)
    assert await cached() == (True, True, True)

    # The account becomes a platform administrator: the suspension stops applying.
    await op_store.update_user_fields(member, role="admin")
    assert await cached() == (False, False, False)
    await op_store.update_user_fields(member, role="free")
    assert await cached() == (True, True, True)

    await op_store.set_organization_status("o-itest-sd", "active", actor=PLATFORM)
    assert await cached() == (False, False, False)


async def test_the_answer_for_an_account_is_cached_also_when_it_is_none(store, monkeypatch):
    raw_store, _pool = store
    op_store = CachedOperationalStore(raw_store, InMemoryCache())
    outsider = await _account(op_store, "se1")
    reads = 0
    read = raw_store.get_suspended_organization

    async def counted(user_id, **options):
        nonlocal reads
        reads += 1
        return await read(user_id, **options)

    monkeypatch.setattr(raw_store, "get_suspended_organization", counted)

    assert await op_store.get_suspended_organization(outsider) is None
    assert await op_store.get_suspended_organization(outsider) is None
    assert reads == 1

    # A sign-in asks the database, and leaves the cached answer alone.
    assert await op_store.get_suspended_organization(outsider, fresh=True) is None
    assert reads == 2
    assert await op_store.get_suspended_organization(outsider) is None
    assert reads == 2


# -- delete -------------------------------------------------------------------


async def test_a_delete_suspends_the_members_and_removes_the_organization(store, monkeypatch):
    raw_store, pool = store
    op_store = CachedOperationalStore(raw_store, InMemoryCache())
    member = await _account(op_store, "da1")
    organization_admin = await _account(op_store, "da2")
    platform_admin = await _account(op_store, "da3", role="admin")
    pending = await _account(op_store, "da4", status="pending_approval")
    suspended_before = await _account(op_store, "da5")
    outsider = await _account(op_store, "da6")
    # A platform administrator by its ``ADMIN_EMAILS`` address, not yet by its role.
    listed_admin = await _account(op_store, "da7")
    monkeypatch.setattr(config.settings, "admin_emails", "da7@org-itest.example.edu")
    for user_id in (
        member,
        organization_admin,
        platform_admin,
        suspended_before,
        outsider,
        listed_admin,
    ):
        await _key(op_store, user_id)
    sid = await _session(op_store, member)
    await _session(op_store, platform_admin)
    await _session(op_store, outsider)
    await _session(op_store, listed_admin)
    await _organization(
        op_store,
        "da",
        ("itest-da-campus", [member, organization_admin, platform_admin, listed_admin]),
        ("itest-da-staff", [pending, suspended_before]),
    )
    await _member_role(pool, organization_admin, "admin")
    await op_store.suspend_user(suspended_before, suspension_message="Suspended before.")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO spend_counters (scope_id, period, cost_usd) "
            "VALUES ('o:o-itest-da', 'm:2026-11', 3), ('o:o-itest-da', 'm:2026-10', 2)"
        )
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd) VALUES ($1, '2026-11-20', 3)",
            member,
        )
    # What the cached store holds of the member before the delete.
    assert (await op_store.get_user_by_id(member))["status"] == "active"
    assert await op_store.session_is_live(member, sid)
    # A member signs in through single sign-on only: it can have no password.
    assert not await raw_store.set_user_password(member, "a-hash-while-a-member")
    before = await _state(pool, platform_admin, pending, suspended_before, outsider, listed_admin)

    outcome, result = await op_store.delete_organization(
        "o-itest-da",
        confirm_name="Itest da",
        handled_connections=["itest-da-campus", "itest-da-staff"],
        disabled_connections=["itest-da-campus"],
        suspension_message="Itest da was deleted.",
        actor=PLATFORM,
    )

    assert outcome == orgs.DELETED
    assert result == {
        "name": "Itest da",
        "members": 6,
        "suspended_user_ids": sorted([member, organization_admin]),
        "connections": ["itest-da-campus", "itest-da-staff"],
    }
    assert await _organization_rows(pool, "o-itest-da") == {
        "organizations": 0,
        "members": 0,
        "connections": 0,
        "counters": 0,
    }
    assert await op_store.get_provider_organization("itest-da-campus") is None
    after = await _state(pool, member, organization_admin)
    assert [(row["status"], row["suspension_message"]) for row in after["users"]] == [
        ("suspended", "Itest da was deleted."),
        ("suspended", "Itest da was deleted."),
    ]
    assert {row["status"] for row in after["keys"]} == {"revoked"}
    assert [row["revoked"] for row in after["sessions"]] == [True]
    # A platform administrator by its role and one by its address, an account
    # that waits for approval, an account that was suspended before, and an
    # account of no organization: unchanged.
    unchanged = await _state(
        pool, platform_admin, pending, suspended_before, outsider, listed_admin
    )
    assert unchanged["users"] == before["users"]
    assert unchanged["keys"] == before["keys"]
    assert unchanged["sessions"] == before["sessions"]
    assert unchanged["members"] == []
    # The cached store does not answer from before the delete.
    assert (await op_store.get_user_by_id(member))["status"] == "suspended"
    assert not await op_store.session_is_live(member, sid)
    # The usage rows of the accounts stay, and so do the audit rows.
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT COUNT(*) FROM user_daily_cost WHERE user_id = $1", member)
            == 1
        )
    rows = await _audit(pool, "o-itest-da")
    assert [row["action"] for row in rows] == ["organization.create", "organization.delete"]
    assert (rows[1]["actor_type"], rows[1]["actor_id"]) == ("admin", "u-org-root")
    assert (rows[1]["target_type"], rows[1]["target_id"]) == ("organization", "o-itest-da")
    assert json.loads(rows[1]["details"]) == {
        "name": "Itest da",
        "members": 6,
        "accounts_suspended": 2,
        "connections": ["itest-da-campus", "itest-da-staff"],
        "connections_disabled": ["itest-da-campus"],
    }
    # The account is in no organization now, so the rule of single sign-on only
    # does not hold it any more: its owner can set a password again.
    assert await raw_store.set_user_password(member, "a-hash-after-the-delete")


async def test_a_delete_is_refused_when_the_organization_is_not_what_the_caller_saw(store):
    op_store, pool = store
    member = await _account(op_store, "db1")
    await _key(op_store, member)
    await _organization(op_store, "db", ("itest-db", [member]), ("itest-db-late", []))
    organization_before = await _organization_rows(pool, "o-itest-db")
    before = await _state(pool, member)

    unknown = await op_store.delete_organization(
        "o-itest-nobody", confirm_name="Itest db", actor=PLATFORM
    )
    other_name = await op_store.delete_organization(
        "o-itest-db", confirm_name="itest db", handled_connections=None, actor=PLATFORM
    )
    # The caller handled one connection; the organization has a second one.
    late_attach = await op_store.delete_organization(
        "o-itest-db", confirm_name="Itest db", handled_connections=["itest-db"], actor=PLATFORM
    )

    assert unknown == (orgs.NOT_FOUND, {})
    assert other_name == (orgs.NAME_CHANGED, {"name": "Itest db"})
    assert late_attach == (
        orgs.CONNECTIONS_CHANGED,
        {"name": "Itest db", "connections": ["itest-db", "itest-db-late"]},
    )
    assert await _organization_rows(pool, "o-itest-db") == organization_before
    assert await _state(pool, member) == before
    assert [row["action"] for row in await _audit(pool, "o-itest-db")] == ["organization.create"]

    # A connection that was detached since the caller read them does not stop the delete.
    outcome, _result = await op_store.delete_organization(
        "o-itest-db",
        confirm_name="Itest db",
        handled_connections=["itest-db", "itest-db-late", "itest-db-gone"],
        actor=PLATFORM,
    )

    assert outcome == orgs.DELETED


async def test_a_failure_inside_a_delete_leaves_every_row_as_it_was(store, monkeypatch):
    op_store, pool = store
    member = await _account(op_store, "dc1")
    await _key(op_store, member)
    await _session(op_store, member)
    await _organization(op_store, "dc", ("itest-dc", [member]))
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO spend_counters (scope_id, period, cost_usd) "
            "VALUES ('o:o-itest-dc', 'm:2026-11', 3)"
        )
    organization_before = await _organization_rows(pool, "o-itest-dc")
    before = await _state(pool, member)
    write_audit_row = orgs.insert_audit_row

    async def fail_on_the_delete(conn, **row):
        # The last statement of the transaction: everything else was written.
        if row["action"] == "organization.delete":
            raise RuntimeError("the audit row could not be written")
        return await write_audit_row(conn, **row)

    monkeypatch.setattr(orgs, "insert_audit_row", fail_on_the_delete)

    with pytest.raises(RuntimeError, match="audit row"):
        await op_store.delete_organization(
            "o-itest-dc",
            confirm_name="Itest dc",
            handled_connections=["itest-dc"],
            suspension_message="Itest dc was deleted.",
            actor=PLATFORM,
        )

    assert organization_before == {
        "organizations": 1,
        "members": 1,
        "connections": 1,
        "counters": 1,
    }
    assert await _organization_rows(pool, "o-itest-dc") == organization_before
    assert await _state(pool, member) == before
    assert before["users"][0]["status"] == "active"
    assert [row["action"] for row in await _audit(pool, "o-itest-dc")] == ["organization.create"]


async def _held_delete(op_store, monkeypatch, organization: str, **arguments):
    """Start a delete of *organization* that stops before its last statement.

    Returns the task, and the event that lets it finish. The delete holds its
    locks and has suspended the members; nothing is committed.
    """
    write_audit_row = orgs.insert_audit_row
    reached, go_on = asyncio.Event(), asyncio.Event()

    async def write_then_wait(conn, **row):
        if row["action"] == "organization.delete":
            reached.set()
            await go_on.wait()
        return await write_audit_row(conn, **row)

    monkeypatch.setattr(orgs, "insert_audit_row", write_then_wait)
    task = asyncio.create_task(
        op_store.delete_organization(
            f"o-itest-{organization}",
            confirm_name=f"Itest {organization}",
            suspension_message="Deleted.",
            actor=PLATFORM,
            **arguments,
        )
    )
    await asyncio.wait_for(reached.wait(), 10)
    return task, go_on


async def test_a_key_that_is_made_while_a_delete_runs_is_refused(store, monkeypatch):
    """The create waits for the lock, and then finds the account suspended."""
    op_store, pool = store
    member = await _account(op_store, "dd1")
    outsider = await _account(op_store, "dd2")
    await _key(op_store, member)
    await _organization(op_store, "dd", ("itest-dd", [member]))

    delete, go_on = await _held_delete(op_store, monkeypatch, "dd")
    try:
        create = asyncio.create_task(_key(op_store, member))
        waited = await _waits_for_the_other(pool, create)
    finally:
        go_on.set()

    assert (await delete)[0] == orgs.DELETED
    with pytest.raises(KeyOwnerSuspendedError):
        await create
    assert waited
    async with pool.acquire() as conn:
        live = await conn.fetchval(
            "SELECT COUNT(*) FROM api_keys WHERE user_id = $1 AND status = 'active'", member
        )
    assert live == 0
    # An account that is not suspended gets its key as before.
    assert await _key(op_store, outsider)


async def test_a_key_is_refused_for_a_suspended_account_only(store):
    op_store, pool = store
    suspended = await _account(op_store, "de1")
    pending = await _account(op_store, "de2", status="pending_approval")
    await op_store.suspend_user(suspended)

    with pytest.raises(KeyOwnerSuspendedError):
        await _key(op_store, suspended)
    # An administrator mints a key for an account that waits for approval, as before.
    await _key(op_store, pending)

    async with pool.acquire() as conn:
        keys = await conn.fetch(
            "SELECT user_id, status FROM api_keys WHERE user_id = ANY($1::text[])",
            [suspended, pending],
        )
    assert [(row["user_id"], row["status"]) for row in keys] == [(pending, "active")]


@pytest.mark.parametrize("kind", ["first", "link"])
async def test_a_sign_in_that_waits_for_a_delete_makes_no_member(store, monkeypatch, kind):
    op_store, pool = store
    member = await _account(op_store, "df1")
    if kind == "link":
        await _account(op_store, "df2")
    await _organization(op_store, "df", ("itest-df", [member]))

    delete, go_on = await _held_delete(op_store, monkeypatch, "df")
    try:
        sign_in = asyncio.create_task(_sign_in(op_store, kind, "df2", "itest-df"))
        waited = await _waits_for_the_other(pool, sign_in)
    finally:
        go_on.set()

    assert (await delete)[0] == orgs.DELETED
    # The sign-in did not fail on a membership of an organization that is gone.
    await sign_in
    assert waited
    assert await op_store.get_organization_membership("u-org-df2") is None
    assert (await _organization_rows(pool, "o-itest-df"))["members"] == 0


async def test_an_attach_that_waits_for_a_delete_finds_no_organization(store, monkeypatch):
    op_store, pool = store
    await _organization(op_store, "dg")

    delete, go_on = await _held_delete(op_store, monkeypatch, "dg")
    try:
        attach = asyncio.create_task(
            op_store.attach_organization_connection(
                "o-itest-dg", "itest-dg", accounts_of=lambda _identities: [], actor=PLATFORM
            )
        )
        waited = await _waits_for_the_other(pool, attach)
    finally:
        go_on.set()

    assert (await delete)[0] == orgs.DELETED
    assert await attach == (orgs.NOT_FOUND, 0)
    assert waited
    assert await op_store.get_provider_organization("itest-dg") is None


async def test_the_second_of_two_deletes_at_once_finds_no_organization(store, monkeypatch):
    op_store, pool = store
    member = await _account(op_store, "dh1")
    await _organization(op_store, "dh", ("itest-dh", [member]))

    first, go_on = await _held_delete(op_store, monkeypatch, "dh")
    try:
        second = asyncio.create_task(
            op_store.delete_organization("o-itest-dh", confirm_name="Itest dh", actor=PLATFORM)
        )
        waited = await _waits_for_the_other(pool, second)
    finally:
        go_on.set()

    assert (await first)[0] == orgs.DELETED
    assert await second == (orgs.NOT_FOUND, {})
    assert waited
    actions = [row["action"] for row in await _audit(pool, "o-itest-dh")]
    assert actions == ["organization.create", "organization.delete"]


async def _waits_for_a_row(pool, task: asyncio.Task) -> bool:
    """Whether *task* comes to wait for a row another transaction holds, rather than to its end."""
    for _ in range(500):
        if task.done():
            return False
        async with pool.acquire() as conn:
            waiting = await conn.fetchval(
                "SELECT 1 FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
                "WHERE l.locktype IN ('transactionid', 'tuple') AND NOT l.granted "
                "AND d.datname = current_database()"
            )
            if waiting is None:
                # A wait for a transaction to end has no database of its own.
                waiting = await conn.fetchval(
                    "SELECT 1 FROM pg_locks WHERE locktype = 'transactionid' AND NOT granted"
                )
        if waiting:
            return True
        await asyncio.sleep(0.01)
    return False


async def test_a_member_that_is_restored_while_the_delete_runs_is_suspended_with_the_others(
    store, monkeypatch
):
    """The delete waits for the restore, and then reads the account as active."""
    op_store, pool = store
    member = await _account(op_store, "di1")
    other = await _account(op_store, "di2")
    await _organization(op_store, "di", ("itest-di", [member, other]))
    assert (
        await op_store.remove_organization_member("o-itest-di", member, actor=shared.ORG_ADMIN)
        == orgs.UPDATED
    )
    write_audit_row = orgs.insert_audit_row
    reached, go_on = asyncio.Event(), asyncio.Event()

    async def write_then_wait(conn, **row):
        if row["action"] == "organization.member.restore":
            # The account is active in this transaction, and nothing is committed.
            reached.set()
            await go_on.wait()
        return await write_audit_row(conn, **row)

    monkeypatch.setattr(orgs, "insert_audit_row", write_then_wait)
    restore = asyncio.create_task(
        op_store.restore_organization_member("o-itest-di", member, actor=shared.ORG_ADMIN)
    )
    try:
        await asyncio.wait_for(reached.wait(), 10)
        delete = asyncio.create_task(
            op_store.delete_organization(
                "o-itest-di", confirm_name="Itest di", suspension_message="Deleted.", actor=PLATFORM
            )
        )
        waited = await _waits_for_a_row(pool, delete)
    finally:
        go_on.set()

    assert await restore == orgs.UPDATED
    outcome, result = await delete
    assert outcome == orgs.DELETED
    assert waited
    # Both members are suspended, also the one whose restore committed first.
    assert result["suspended_user_ids"] == sorted([member, other])
    state = await _state(pool, member, other)
    assert [row["status"] for row in state["users"]] == ["suspended", "suspended"]
    assert state["members"] == []
