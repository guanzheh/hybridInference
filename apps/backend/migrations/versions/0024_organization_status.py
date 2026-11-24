"""Organizations: the status of an organization.

``orgs.status`` is ``active`` or ``suspended``. A new organization
and every organization that exists is ``active``. The gateway reads the status
where it authenticates an account, and refuses the members of a suspended
organization. A deleted organization has no row, so there is no third value.

When and by whom an organization was suspended is in its audit rows
(``organization.suspend``, ``organization.restore``); no column holds it.

Expand only: earlier code does not read the column, and the default gives every
row it writes the value it means.

Revision ID: 0024_organization_status
Revises: 0023_orgs
Create Date: 2026-11-21
"""

from __future__ import annotations

from alembic import op

revision = "0024_organization_status"
down_revision = "0023_orgs"
branch_labels = None
depends_on = None
compatibility = "expand"


def upgrade() -> None:
    """Add the status column, ``active`` for every organization."""
    op.execute(
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL "
        "DEFAULT 'active' CHECK (status IN ('active', 'suspended'))"
    )


def downgrade() -> None:
    """Keep the column: earlier code does not name it.

    A suspended organization stays suspended in its row, and the gateway that
    reads the column refuses its members again after the next upgrade.
    """
