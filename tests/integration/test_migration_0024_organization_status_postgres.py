"""``0024_organization_status``: the status of an organization."""

from __future__ import annotations

import asyncpg
import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

VERSION_TABLE = "hybridinference_alembic_version"


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_every_organization_that_exists_becomes_active(scratch):
    await mig.upgrade(scratch, "0023_orgs")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO orgs (id, name, monthly_limit_usd) VALUES ('o-1', 'Example One', 1000)"
        )
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-1', 'a@example.edu')")
        await conn.execute(
            "INSERT INTO org_memberships (user_id, organization_id) VALUES ('u-1', 'o-1')"
        )

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        organization = await conn.fetchrow(
            "SELECT name, status, monthly_limit_usd FROM orgs WHERE id = 'o-1'"
        )
        members = await conn.fetchval("SELECT COUNT(*) FROM org_memberships")

    assert organization["status"] == "active"
    assert (organization["name"], organization["monthly_limit_usd"]) == ("Example One", 1000)
    assert members == 1


async def test_a_write_of_the_previous_release_still_works(scratch):
    """The previous release names no status: its insert and its update must pass."""
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute("INSERT INTO orgs (id, name) VALUES ('o-1', 'Example One')")
        await conn.execute("UPDATE orgs SET name = 'Example', updated_at = NOW() WHERE id = 'o-1'")
        row = await conn.fetchrow("SELECT name, status FROM orgs WHERE id = 'o-1'")

    assert dict(row) == {"name": "Example", "status": "active"}


async def test_the_status_has_two_values(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute("INSERT INTO orgs (id, name) VALUES ('o-1', 'Example One')")
        await conn.execute("UPDATE orgs SET status = 'suspended' WHERE id = 'o-1'")
        suspended = await conn.fetchval("SELECT status FROM orgs WHERE id = 'o-1'")

        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE orgs SET status = 'deleted' WHERE id = 'o-1'")
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute("UPDATE orgs SET status = NULL WHERE id = 'o-1'")

    assert suspended == "suspended"


async def test_the_revision_can_run_twice(scratch):
    """A database that already has the column (a downgrade keeps it) upgrades again."""
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute("INSERT INTO orgs (id, name) VALUES ('o-1', 'Example One')")
        await conn.execute("UPDATE orgs SET status = 'suspended' WHERE id = 'o-1'")
        await conn.execute(
            f"UPDATE {VERSION_TABLE} SET version_num = '0023_orgs' "
            "WHERE version_num = '0024_organization_status'"
        )

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        status = await conn.fetchval("SELECT status FROM orgs WHERE id = 'o-1'")
        checks = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_constraint "
            "WHERE conrelid = 'orgs'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%status%'"
        )

    assert status == "suspended"
    assert checks == 1
