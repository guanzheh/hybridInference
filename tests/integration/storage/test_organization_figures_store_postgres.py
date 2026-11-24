"""The figures of the list of organizations, in the store, against real PostgreSQL.

The rows are tagged as in ``test_organizations_store_postgres`` (``o-itest-*``
organizations, ``u-org-*`` accounts, ``itest-*`` providers), whose fixture and
helpers these tests use.
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta

import pytest

from serving.storage import user_usage_rollup
from tests.integration.storage import test_organizations_store_postgres as shared
from tests.integration.storage.test_organizations_store_postgres import _account, _organization

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

store = shared.store

TAG = "org-itest-figures-"


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def _request(conn, user_id: str, at: datetime) -> None:
    await conn.execute(
        "INSERT INTO api_logs (request_id, timestamp, model_id, provider, user_id, "
        "status_code, total_tokens, cost_usd) VALUES ($1, $2, 'model-a', 'test', $3, 200, 10, 0)",
        TAG + secrets.token_hex(6),
        at,
        user_id,
    )


async def test_the_figures_of_a_span_are_those_of_its_days_stored_in_the_rollup_or_not(store):
    op_store, pool = store
    member = await _account(op_store, "fg1")
    other = await _account(op_store, "fg2")
    outsider = await _account(op_store, "fg3")
    await _organization(op_store, "fg", ("itest-fg", [member, other]))
    today = datetime.now(UTC).date()
    first, last = today - timedelta(days=5), today - timedelta(days=3)
    second = timedelta(seconds=1)
    days = [first - timedelta(days=1), first, first + timedelta(days=1), last, today - timedelta(2)]

    async def figures() -> tuple[int, float]:
        rows = await op_store.list_organizations(first_day=first, last_day=last)
        [row] = [row for row in rows if row["id"] == "o-itest-fg"]
        return row["requests"], float(row["cost_usd"])

    try:
        async with pool.acquire() as conn:
            # The member: one request at each edge of the span, and one on each side of it.
            for at in (
                _midnight(first) - second,
                _midnight(first),
                _midnight(first + timedelta(days=1)) + timedelta(hours=12),
                _midnight(last + timedelta(days=1)) - second,
                _midnight(last + timedelta(days=1)),
            ):
                await _request(conn, member, at)
            for _ in range(2):
                await _request(conn, other, _midnight(last) + timedelta(hours=3))
            # Today is after the span, and is the part that no rollup holds.
            await _request(conn, member, datetime.now(UTC))
            # An account of no organization, in the span.
            await _request(conn, outsider, _midnight(first) + timedelta(hours=1))
            await conn.executemany(
                "INSERT INTO user_daily_cost (user_id, day, cost_usd) VALUES ($1, $2, $3)",
                [
                    (member, (first - timedelta(days=1)).isoformat(), 1),
                    (member, first.isoformat(), 2),
                    (other, last.isoformat(), 4),
                    (member, (last + timedelta(days=1)).isoformat(), 8),
                    (outsider, first.isoformat(), 16),
                ],
            )
        # Three requests of the member and two of the other; the spend of two days.
        inside = (5, 6.0)

        # No day is in the rollup: each is counted from the request log.
        async with pool.acquire() as conn, conn.transaction():
            await user_usage_rollup.invalidate_days(conn, days)
        assert await figures() == inside
        # Each day is in the rollup.
        async with pool.acquire() as conn:
            await user_usage_rollup.fill_days(conn, days)
        assert await figures() == inside
        # One day of the span is in the rollup no more, and the others are.
        async with pool.acquire() as conn, conn.transaction():
            await user_usage_rollup.invalidate_days(conn, [first + timedelta(days=1)])
        assert await figures() == inside
    finally:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE $1", TAG + "%")
            await conn.execute(
                "DELETE FROM user_daily_cost WHERE user_id = ANY($1::text[])",
                [member, other, outsider],
            )
            # The days this test stored hold rows that are gone now.
            await user_usage_rollup.invalidate_days(conn, days)
            await conn.execute(
                "DELETE FROM user_usage_daily WHERE user_id = ANY($1::text[])",
                [member, other, outsider],
            )


async def test_an_organization_with_no_usage_has_zero_figures_and_its_administrators(store):
    op_store, pool = store
    administrator = await _account(op_store, "fz1")
    await _organization(op_store, "fz", ("itest-fz", [administrator]))
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE org_memberships SET role = 'admin' WHERE user_id = $1", administrator
        )
    today = datetime.now(UTC).date()

    rows = await op_store.list_organizations(first_day=today, last_day=today)
    without_a_span = await op_store.list_organizations()

    [row] = [row for row in rows if row["id"] == "o-itest-fz"]
    assert (row["requests"], row["cost_usd"], row["spent_month_usd"]) == (0, 0, 0)
    # Asked for no span, a row has no usage, and all that it has otherwise.
    [plain] = [row for row in without_a_span if row["id"] == "o-itest-fz"]
    assert plain == {
        key: value
        for key, value in row.items()
        if key not in ("requests", "cost_usd", "cost_amount")
    }
    assert (row["member_count"], row["status"]) == (1, "active")
    assert [person["user_id"] for person in row["administrators"]] == [administrator]
    assert "organization_id" not in row["administrators"][0]
