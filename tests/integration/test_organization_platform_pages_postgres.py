"""The platform administrator's side of an organization, over HTTP against the real stores.

The list of organizations with the figures of a period, and the pages of one
organization: the routes of the organization console, served a second time
below ``/admin/organizations/{organization_id}``.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from serving.servers.routers.admin import organization_pages
from serving.storage import user_usage_rollup
from tests.fixtures.sso_app import existing_user, new_browser
from tests.integration import (
    test_organization_member_access_postgres as access,
    test_organizations_postgres as shared,
)
from tests.integration.test_organizations_postgres import (
    PLATFORM,
    _make_organization,
    _organization_with_an_administrator,
    _platform_admin,
)

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

organization_routes = shared.organization_routes
clean_organizations = shared.clean_organizations
members_sign_straight_in = shared.members_sign_straight_in
deployment_models = access.deployment_models

CONSOLE = "/user/organization"
FIGURES = ("cost_usd", "requests", "member_count", "spent_month_usd")


async def _usage(pool, user_id: str, *, requests: int, cost: str, at: datetime) -> None:
    """Logged requests of an account at *at*, and its cost counter for that UTC day."""
    async with pool.acquire() as conn:
        for index in range(requests):
            await conn.execute(
                "INSERT INTO api_logs (request_id, timestamp, model_id, provider, user_id, "
                "status_code, prompt_tokens, completion_tokens, total_tokens, cost_usd) "
                "VALUES ($1, $2, 'model-a', 'test', $3, $4, 100, 50, 150, $5)",
                f"org-itest-{secrets.token_hex(6)}",
                at,
                user_id,
                # One request of each account failed: the list counts it too.
                200 if index else 500,
                float(cost) / requests,
            )
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests, last_request_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id,
            at.date().isoformat(),
            float(cost),
            requests,
            at,
        )


async def _month_counter(pool, organization_id: str, cost: float) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO spend_counters (scope_id, period, cost_usd) "
            "VALUES ($1, 'm:' || to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM'), $2)",
            f"o:{organization_id}",
            cost,
        )


async def _join(pool, organization_id: str, *user_ids: str) -> None:
    async with pool.acquire() as conn:
        for user_id in user_ids:
            await conn.execute(
                "INSERT INTO org_memberships (user_id, organization_id) VALUES ($1, $2)",
                user_id,
                organization_id,
            )


async def _listed(browser, headers, query: str = "") -> dict[str, Any]:
    answered = await browser.get(f"/admin/organizations?{query}", headers=headers)
    assert answered.status_code == 200, answered.text
    return answered.json()


def _names(listed: dict[str, Any]) -> list[str]:
    return [row["name"] for row in listed["organizations"]]


# -- the list ----------------------------------------------------------------------


async def test_the_list_gives_the_figures_that_the_overview_of_an_organization_gives(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    """One period, two readers: the row of the list and the Overview of the organization."""
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    alice, alice_headers, alice_id = state["alice"]
    bob_id = state["bob"][2]
    organization_id = state["organization_id"]
    outsider_id = await existing_user(store, "someone@elsewhere.org")
    now = datetime.now(UTC)
    earlier = [now - timedelta(days=2), now - timedelta(days=10)]
    await _usage(clean_organizations, bob_id, requests=3, cost="1.50", at=now)
    await _usage(clean_organizations, alice_id, requests=2, cost="0.50", at=earlier[0])
    await _usage(clean_organizations, bob_id, requests=4, cost="2.00", at=earlier[1])
    # An account of no organization is in no row.
    await _usage(clean_organizations, outsider_id, requests=5, cost="9.00", at=now)
    await _month_counter(clean_organizations, organization_id, 3.25)
    days = [moment.date() for moment in earlier]
    expected = {"1d": (1.5, 3), "7d": (2.0, 5), "30d": (4.0, 9)}

    async def both(period: str) -> tuple[dict, dict]:
        [row] = (await _listed(root, root_headers, f"period={period}"))["organizations"]
        overview = await alice.get(f"{CONSOLE}/overview?period={period}", headers=alice_headers)
        assert overview.status_code == 200, overview.text
        return {key: row[key] for key in FIGURES}, {key: overview.json()[key] for key in FIGURES}

    async def compare() -> None:
        for period, (cost, requests) in expected.items():
            in_the_list, in_the_overview = await both(period)
            assert in_the_list == in_the_overview, period
            assert in_the_list == {
                "cost_usd": cost,
                "requests": requests,
                "member_count": 2,
                "spent_month_usd": 3.25,
            }, period

    # The earlier days are not in the rollup: the list counts them from the log.
    async with clean_organizations.acquire() as conn, conn.transaction():
        await user_usage_rollup.invalidate_days(conn, days)
    await compare()
    try:
        # The same figures when the rollup holds those days.
        async with clean_organizations.acquire() as conn:
            assert await user_usage_rollup.fill_days(conn, days) == len(days)
        await compare()
    finally:
        # The rows of this test go after it: the days it stored would then hold them still.
        async with clean_organizations.acquire() as conn, conn.transaction():
            await user_usage_rollup.invalidate_days(conn, days)


async def test_the_list_has_totals_shares_status_search_order_and_pages(
    app, store, clean_organizations, sso
):
    root, headers, _ = await _platform_admin(app, store)
    # Before an organization has it, the connection can be attached to one.
    before = await _listed(root, headers)
    assert before["organizations"] == [] and before["total"] == 0
    assert before["available_connections"] == [
        {
            "provider_id": "example-univ",
            "display_name": sso.providers[0]["display_name"],
            "domains": ["example.edu"],
            "enabled": True,
        }
    ]

    university = (await _make_organization(root, headers))["organization"]["id"]
    for organization_id, name in (("o-beta", "Beta College"), ("o-gamma", "Gamma Institute")):
        await store.create_organization(organization_id=organization_id, name=name, actor=PLATFORM)
    one = await existing_user(store, "one@example.edu")
    two = await existing_user(store, "two@elsewhere.org")
    three = await existing_user(store, "three@elsewhere.org")
    await _join(clean_organizations, university, one)
    await _join(clean_organizations, "o-beta", two, three)
    now = datetime.now(UTC)
    await _usage(clean_organizations, one, requests=2, cost="1.00", at=now)
    await _usage(clean_organizations, two, requests=6, cost="3.00", at=now)
    await _month_counter(clean_organizations, university, 5.0)
    await _month_counter(clean_organizations, "o-beta", 1.0)
    suspended = await root.post("/admin/organizations/o-gamma/suspend", headers=headers)
    assert suspended.status_code == 200, suspended.text

    everything = await _listed(root, headers, "period=7d")

    assert _names(everything) == ["Beta College", "Example University", "Gamma Institute"]
    assert everything["total"] == 3 and everything["period"] == "7d"
    totals = {
        "organizations": 3,
        "members": 3,
        "requests": 8,
        "cost_usd": 4.0,
    }
    assert {key: everything["totals"][key] for key in totals} == totals
    assert everything["status_counts"] == {"active": 2, "suspended": 1}
    assert everything["available_connections"] == []
    beta, example, gamma = everything["organizations"]
    assert (beta["spend_share"], example["spend_share"], gamma["spend_share"]) == (0.75, 0.25, 0)
    assert (example["domains"], beta["domains"]) == (["example.edu"], [])
    assert (gamma["status"], gamma["requests"], gamma["member_count"]) == ("suspended", 0, 0)
    # A row keeps what the route of one organization has.
    alone = (await root.get(f"/admin/organizations/{university}", headers=headers)).json()
    assert {key: example[key] for key in alone} == alone

    # The status and the search narrow the rows and `total`, and no figure above them.
    only_suspended = await _listed(root, headers, "period=7d&status=suspended")
    by_name = await _listed(root, headers, "period=7d&search=beta")
    by_domain = await _listed(root, headers, "period=7d&search=EXAMPLE.EDU")
    assert (_names(only_suspended), only_suspended["total"]) == (["Gamma Institute"], 1)
    assert (_names(by_name), _names(by_domain)) == (["Beta College"], ["Example University"])
    for narrowed in (only_suspended, by_name, by_domain):
        assert narrowed["totals"] == everything["totals"]
        assert narrowed["status_counts"] == everything["status_counts"]

    # Each order, both ways. Rows with one figure stay in the order of their names.
    orders = {
        "name": ["Beta College", "Example University", "Gamma Institute"],
        "members": ["Gamma Institute", "Example University", "Beta College"],
        "requests": ["Gamma Institute", "Example University", "Beta College"],
        "spend": ["Gamma Institute", "Example University", "Beta College"],
        "month_spend": ["Gamma Institute", "Beta College", "Example University"],
    }
    for sort_by, ascending in orders.items():
        up = await _listed(root, headers, f"period=7d&sort_by={sort_by}&order=asc")
        down = await _listed(root, headers, f"period=7d&sort_by={sort_by}&order=desc")
        assert _names(up) == ascending, sort_by
        assert _names(down) == ascending[::-1], sort_by

    page = await _listed(root, headers, "period=7d&limit=1&offset=1")
    assert (_names(page), page["total"]) == (["Example University"], 3)
    refused = await root.get("/admin/organizations?sort_by=secret", headers=headers)
    assert refused.status_code == 422


async def test_the_list_without_a_period_is_each_organization_and_no_usage(
    app, store, clean_organizations, sso, monkeypatch
):
    """As a caller reads it that wants the organizations for a filter, and sends no parameter."""
    root, headers, _ = await _platform_admin(app, store)
    university = (await _make_organization(root, headers))["organization"]["id"]
    for number in range(1, 61):
        await store.create_organization(
            organization_id=f"o-{number:02}", name=f"College {number:02}", actor=PLATFORM
        )
    member = await existing_user(store, "one@example.edu")
    await _join(clean_organizations, university, member)
    await _usage(clean_organizations, member, requests=2, cost="1.00", at=datetime.now(UTC))
    sent: list[str] = []
    real = asyncpg.Connection.fetch

    async def fetch(self, query, *args, **kwargs):
        sent.append(query)
        return await real(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "fetch", fetch)

    plain = await _listed(root, headers)

    # Each organization, and not a page of them.
    assert (len(plain["organizations"]), plain["total"]) == (61, 61)
    assert _names(plain)[:2] == ["College 01", "College 02"]
    # No usage: not in a row, not in the totals, and none was read.
    [example] = [row for row in plain["organizations"] if row["id"] == university]
    assert (example["requests"], example["cost_usd"], example["spend_share"]) == (None,) * 3
    assert (example["member_count"], example["domains"]) == (1, ["example.edu"])
    assert plain["period"] is None
    assert (plain["totals"]["organizations"], plain["totals"]["members"]) == (61, 1)
    assert (plain["totals"]["requests"], plain["totals"]["cost_usd"]) == (None, None)
    assert not [query for query in sent if "user_daily_cost" in query or "api_logs" in query]
    # A row has what the route of one organization has.
    alone = (await root.get(f"/admin/organizations/{university}", headers=headers)).json()
    assert {key: example[key] for key in alone} == alone
    # An order that is a figure of a period needs the period.
    for figure in ("requests", "spend"):
        refused = await root.get(f"/admin/organizations?sort_by={figure}", headers=headers)
        assert refused.status_code == 400, refused.text
    by_members = await _listed(root, headers, "sort_by=members&order=desc&limit=1")
    assert _names(by_members) == ["Example University"]


async def test_the_list_reads_as_many_statements_for_twenty_organizations_as_for_one(
    store, clean_organizations, monkeypatch
):
    span = {"first_day": datetime.now(UTC).date() - timedelta(days=6)}
    span["last_day"] = datetime.now(UTC).date()
    sent: list[str] = []

    def counted(name: str):
        real = getattr(asyncpg.Connection, name)

        async def call(self, query, *args, **kwargs):
            sent.append(query)
            return await real(self, query, *args, **kwargs)

        return call

    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        monkeypatch.setattr(asyncpg.Connection, name, counted(name))

    async def statements(**days) -> tuple[int, int, int]:
        """How many statements one read sends, how many of them read a table, and its rows."""
        sent.clear()
        rows = await store.list_organizations(**days)
        return len(sent), sum(" FROM " in query for query in sent), len(rows)

    await store.create_organization(organization_id="o-1", name="Organization 1", actor=PLATFORM)
    for_one = await statements(**span)
    for number in range(2, 21):
        await store.create_organization(
            organization_id=f"o-{number}", name=f"Organization {number}", actor=PLATFORM
        )
        member = await existing_user(store, f"member{number}@elsewhere.org")
        await _join(clean_organizations, f"o-{number}", member)
        await _usage(clean_organizations, member, requests=1, cost="0.10", at=datetime.now(UTC))
    for_twenty = await statements(**span)
    without_a_span = await statements()

    assert (for_one[2], for_twenty[2], without_a_span[2]) == (1, 20, 20)
    assert for_twenty[:2] == for_one[:2]
    # One read for each source: the organizations, the spend of the period, the
    # requests, the counters of the month and the administrators. The other
    # statements are the transaction around the read of the rollup, and the pool.
    assert for_one[1] == 5, [query.strip()[:60] for query in sent]
    # Without a span, the two reads of the usage are not made.
    assert without_a_span[1] == 3


# -- the pages of one organization -------------------------------------------------


def _path(route, organization_id: str, user_id: str, key_id: int) -> str:
    return route.path.format(organization_id=organization_id, user_id=user_id, key_id=key_id)


def _body(method: str, path: str) -> dict[str, Any] | None:
    """A body that the route accepts; None for a route that takes none."""
    if method != "PUT":
        return None
    if path.endswith("/models"):
        return {"disabled_models": []}
    return {"monthly_limit_usd": 40}


async def _send(browser, method: str, path: str, headers: dict | None = None):
    return await browser.request(method, path, json=_body(method, path), headers=headers)


async def _page_state(app, idp, store, pool) -> dict[str, Any]:
    state = await _organization_with_an_administrator(app, idp, store)
    bob_id = state["bob"][2]
    await _usage(pool, bob_id, requests=3, cost="1.50", at=datetime.now(UTC))
    key = await store.create_key(
        key_hash=secrets.token_hex(16),
        key_prefix=f"sk-{secrets.token_hex(3)}",
        user_id=bob_id,
        account_id=bob_id,
        name="laptop",
        api_key_encrypted="encrypted-key-material",
    )
    return {**state, "key_id": key["id"]}


async def test_each_page_answers_the_platform_administrator_as_it_answers_the_organization(
    app, idp, store, clean_organizations, members_sign_straight_in, deployment_models
):
    state = await _page_state(app, idp, store, clean_organizations)
    root, root_headers = state["root"]
    alice, alice_headers, _ = state["alice"]
    organization_id, bob_id, key_id = state["organization_id"], state["bob"][2], state["key_id"]
    prefix = f"/admin/organizations/{organization_id}"
    reads = [
        _path(route, organization_id, bob_id, key_id)
        for route in organization_pages.router.routes
        if route.methods == {"GET"} and not route.path.endswith("/audit-log")
    ]
    assert len(reads) == 9

    for path in reads:
        for_the_platform = await root.get(path, headers=root_headers)
        for_the_organization = await alice.get(
            CONSOLE + path.removeprefix(prefix), headers=alice_headers
        )
        assert for_the_platform.status_code == 200, (path, for_the_platform.text)
        assert for_the_platform.json() == for_the_organization.json(), path

    # A suspension refuses the administrator of the organization, and none of these routes.
    await root.post(f"{prefix}/suspend", headers=root_headers)
    for path in reads:
        assert (await root.get(path, headers=root_headers)).status_code == 200, path
    limited = await root.put(
        f"{prefix}/members/{bob_id}/monthly-limit",
        json={"monthly_limit_usd": 7},
        headers=root_headers,
    )
    assert limited.status_code == 200, limited.text
    assert (await alice.get(f"{CONSOLE}/overview", headers=alice_headers)).status_code == 403


async def test_each_route_of_one_organization_is_the_platform_administrators_alone(
    app, idp, store, clean_organizations, members_sign_straight_in, deployment_models
):
    state = await _page_state(app, idp, store, clean_organizations)
    root, root_headers = state["root"]
    alice, alice_headers, _ = state["alice"]
    bob, bob_headers, bob_id = state["bob"]
    organization_id, key_id = state["organization_id"], state["key_id"]
    routes = [(sorted(route.methods)[0], route) for route in organization_pages.router.routes]
    assert len(routes) == 16

    for method, route in routes:
        path = _path(route, organization_id, bob_id, key_id)
        nobody = await _send(new_browser(app), method, path)
        member = await _send(bob, method, path, bob_headers)
        its_administrator = await _send(alice, method, path, alice_headers)
        nowhere = await _send(
            root, method, _path(route, "no-such-organization", bob_id, key_id), root_headers
        )
        assert (
            nobody.status_code,
            member.status_code,
            its_administrator.status_code,
            nowhere.status_code,
        ) == (401, 403, 403, 404), (method, path)

    # Nothing was changed by a caller that was refused.
    assert (await store.get_user_by_id(bob_id))["status"] == "active"
    # The platform administrator is answered by each of them. The removal is
    # the last write but one: it revokes the keys of the member.
    writes_last = sorted(routes, key=lambda item: item[1].path.endswith(("/remove", "/restore")))
    for method, route in writes_last:
        path = _path(route, organization_id, bob_id, key_id)
        answered = await _send(root, method, path, root_headers)
        assert answered.status_code == 200, (method, path, answered.text)
    assert (await store.get_user_by_id(bob_id))["status"] == "active"


async def test_the_organization_of_the_path_is_the_only_one_that_is_reached(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    bob_id = state["bob"][2]
    await store.create_organization(organization_id="o-other", name="Other College", actor=PLATFORM)
    other = "/admin/organizations/o-other"

    # Bob is a member of the first organization, and is named below the second.
    read = await root.get(f"{other}/members/{bob_id}", headers=root_headers)
    limited = await root.put(
        f"{other}/members/{bob_id}/monthly-limit",
        json={"monthly_limit_usd": 1},
        headers=root_headers,
    )
    removed = await root.post(f"{other}/members/{bob_id}/remove", headers=root_headers)
    listed = await root.get(f"{other}/members", headers=root_headers)

    assert (read.status_code, limited.status_code, removed.status_code) == (404, 404, 404)
    assert listed.json()["total"] == 0
    assert (await store.get_user_by_id(bob_id))["status"] == "active"
    async with clean_organizations.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT monthly_limit_usd FROM org_memberships WHERE user_id = $1", bob_id
            )
            is None
        )


async def test_the_admin_token_reads_the_pages_and_its_writes_name_no_account(
    app, idp, store, clean_organizations, members_sign_straight_in, monkeypatch
):
    from serving.config.settings import get_settings

    monkeypatch.setenv("ADMIN_TOKEN", "org-itest-admin-token")
    get_settings.cache_clear()
    token = {"Authorization": "Bearer org-itest-admin-token"}
    state = await _organization_with_an_administrator(app, idp, store)
    organization_id, bob_id = state["organization_id"], state["bob"][2]
    prefix = f"/admin/organizations/{organization_id}"
    caller = new_browser(app)

    members = await caller.get(f"{prefix}/members", headers=token)
    limited = await caller.put(
        f"{prefix}/members/{bob_id}/monthly-limit", json={"monthly_limit_usd": 9}, headers=token
    )
    log = await caller.get(f"{prefix}/audit-log", headers=token)

    assert (members.status_code, limited.status_code, log.status_code) == (200, 200, 200)
    assert members.json()["total"] == 2
    [entry] = [e for e in log.json()["entries"] if e["action"] == "organization.member_limit.set"]
    assert (entry["actor"], entry["actor_user_id"], entry["actor_email"]) == (
        "platform_administrator",
        None,
        None,
    )
    async with clean_organizations.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT actor_type, actor_id, organization_id FROM admin_audit_log "
            "WHERE action = 'organization.member_limit.set'"
        )
    assert tuple(row) == ("admin_token", None, organization_id)


async def test_what_the_platform_administrator_does_to_a_member_is_in_the_audit_log_of_the_organization(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    alice, alice_headers, alice_id = state["alice"]
    bob_id = state["bob"][2]
    organization_id = state["organization_id"]
    prefix = f"/admin/organizations/{organization_id}"
    root_id = (await store.get_user_by_email("root@elsewhere.org"))["id"]

    limited = await root.put(
        f"{prefix}/members/{bob_id}/monthly-limit",
        json={"monthly_limit_usd": 12},
        headers=root_headers,
    )
    removed = await root.post(f"{prefix}/members/{bob_id}/remove", headers=root_headers)
    assert (limited.status_code, removed.status_code) == (200, 200), (limited.text, removed.text)
    # The member is told who removed it.
    assert (await store.get_user_by_id(bob_id))["suspension_message"] == (
        "Removed from Example University by a platform administrator."
    )
    # The removal is the organization's: its administrator can undo it.
    restored = await alice.post(f"{CONSOLE}/members/{bob_id}/restore", headers=alice_headers)
    assert restored.status_code == 200, restored.text

    async with clean_organizations.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, actor_type, actor_id, organization_id FROM admin_audit_log "
            "WHERE organization_id = $1 AND action LIKE 'organization.member%' ORDER BY id",
            organization_id,
        )
    assert [tuple(row) for row in rows] == [
        ("organization.member_limit.set", "admin", root_id, organization_id),
        ("organization.member.remove", "admin", root_id, organization_id),
        ("organization.member.restore", "user", alice_id, organization_id),
    ]

    def actors(log: dict[str, Any]) -> dict[str, tuple]:
        return {
            entry["action"]: (entry["actor"], entry["actor_user_id"], entry["actor_email"])
            for entry in log["entries"]
            if entry["action"].startswith("organization.member")
        }

    for_the_organization = await alice.get(f"{CONSOLE}/audit-log", headers=alice_headers)
    for_the_platform = await root.get(f"{prefix}/audit-log", headers=root_headers)
    assert for_the_organization.status_code == for_the_platform.status_code == 200

    by_alice = ("member", alice_id, "alice@example.edu")
    # The organization is not shown the account of a platform administrator ...
    assert actors(for_the_organization.json()) == {
        "organization.member_limit.set": ("platform_administrator", None, None),
        "organization.member.remove": ("platform_administrator", None, None),
        "organization.member.restore": by_alice,
    }
    # ... and the platform administrator is.
    by_root = ("platform_administrator", root_id, "root@elsewhere.org")
    assert actors(for_the_platform.json()) == {
        "organization.member_limit.set": by_root,
        "organization.member.remove": by_root,
        "organization.member.restore": by_alice,
    }
    # The search is that of the console: it finds an administrator of the
    # organization by its address, and no platform administrator.
    found = {
        address: (
            await root.get(f"{prefix}/audit-log?search={address}", headers=root_headers)
        ).json()["total"]
        for address in ("alice@example.edu", "root@elsewhere.org")
    }
    assert found["root@elsewhere.org"] == 0 and found["alice@example.edu"] >= 1
    # Apart from that account, the two logs are one.
    without_the_account = [
        {key: value for key, value in entry.items() if not key.startswith("actor_")}
        for entry in for_the_platform.json()["entries"]
    ]
    assert without_the_account == [
        {key: value for key, value in entry.items() if not key.startswith("actor_")}
        for entry in for_the_organization.json()["entries"]
    ]
