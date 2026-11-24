"""The routes of the organization console, served for the platform administrator."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Annotated, get_args
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, Depends, HTTPException, Query

from serving import organizations
from serving.audit import AuditActor
from serving.servers.deps import OrganizationAdmin, require_organization_admin
from serving.servers.routers import organization as console
from serving.servers.routers.admin import (
    organization_pages as pages,
    organizations as admin_routes,
)

#: What a route says beside its path and its parameters. Written here and not
#: read from the module: an option that the module does not copy fails the test.
ROUTE_OPTIONS = (
    "response_model",
    "status_code",
    "name",
    "summary",
    "description",
    "deprecated",
    "include_in_schema",
    "openapi_extra",
    "responses",
    "response_class",
    "response_model_include",
    "response_model_exclude",
    "response_model_by_alias",
    "response_model_exclude_unset",
    "response_model_exclude_defaults",
    "response_model_exclude_none",
)


def _by_tail(router) -> dict[tuple[str, ...], object]:
    return {
        (*sorted(route.methods), route.path.removeprefix(router.prefix)): route
        for route in router.routes
        if route.path != router.prefix
    }


def _parameters(route) -> dict[str, list[tuple]]:
    """What a caller sends to a route: each parameter with its type, default and limits."""
    dependant = route.dependant
    return {
        kind: sorted(
            (
                field.name,
                field.field_info.is_required(),
                str(field.field_info.annotation),
                repr(field.field_info.default),
                # The limits: ``max_length``, ``ge``, ``le``.
                sorted(repr(limit) for limit in field.field_info.metadata),
            )
            for field in getattr(dependant, kind)
        )
        for kind in ("path_params", "query_params", "body_params")
    }


def _scopes(route) -> set:
    return {dependency.call for dependency in route.dependant.dependencies}


def test_each_route_of_the_console_is_served_for_the_platform_administrator():
    originals, twins = _by_tail(console.router), _by_tail(pages.router)

    # Each route but the one that reads the organization itself, and no other.
    assert twins.keys() == originals.keys()
    assert len(console.router.routes) == len(pages.router.routes) + 1
    for tail, original in originals.items():
        twin = twins[tail]
        # What the route says of its answer and of its documentation.
        for name in ROUTE_OPTIONS:
            assert getattr(twin, name) == getattr(original, name), (tail, name)
        # The same parameters: a filter of the console is a filter here.
        assert _parameters(twin) == _parameters(original), tail
        # Only what says where the handler works is different.
        assert require_organization_admin in _scopes(original), tail
        assert pages.organization_of_path in _scopes(twin), tail
        assert require_organization_admin not in _scopes(twin), tail
        assert twin.dependencies == [], tail


def test_a_route_with_a_dependency_that_is_no_parameter_is_not_served():
    """It admitted a member there: nothing says what it is for a platform administrator."""

    async def handler(admin: OrganizationAdmin = Depends(require_organization_admin)) -> dict:
        return {}

    async def audited() -> None:
        return None

    router = APIRouter(prefix=console.router.prefix)
    router.add_api_route("/extra", handler, methods=["GET"], dependencies=[Depends(audited)])
    before = len(pages.router.routes)

    with pytest.raises(TypeError, match="dependencies of its route"):
        pages.serve_for_the_platform(router.routes[0])

    assert len(pages.router.routes) == before


def test_a_handler_that_works_in_no_organization_is_not_served():
    async def handler(period: str = "7d") -> dict:
        return {}

    with pytest.raises(TypeError, match="require_organization_admin"):
        pages.for_the_organization_of_the_path(handler)


def test_a_handler_that_declares_its_parameters_with_annotated_is_copied_whole():
    """The other way to declare a dependency and a limit: neither is lost in the copy."""

    async def handler(
        admin: Annotated[OrganizationAdmin, Depends(require_organization_admin)],
        search: Annotated[str | None, Query(max_length=5)] = None,
    ) -> dict:
        return {}

    router = APIRouter()
    router.add_api_route("/original", handler, methods=["GET"])
    router.add_api_route("/twin", pages.for_the_organization_of_the_path(handler), methods=["GET"])
    original, twin = router.routes

    assert _parameters(twin) == _parameters(original)
    [search] = _parameters(twin)["query_params"]
    assert search[0] == "search" and "max_length=5" in search[4][0]
    assert _scopes(original) == {require_organization_admin}
    assert _scopes(twin) == {pages.organization_of_path}


@pytest.mark.asyncio
async def test_the_organization_of_the_path_is_where_the_platform_administrator_works():
    actor = AuditActor.admin("u-root", "192.0.2.1")
    store = SimpleNamespace(
        get_organization=AsyncMock(return_value={"id": "o1", "name": "Example University"})
    )

    # ``verify_admin_access`` names the caller by its address, which is no account id.
    scope = await pages.organization_of_path(
        "o1", _admin_id="root@elsewhere.org", actor=actor, op_store=store
    )

    assert (scope.organization_id, scope.organization_name) == ("o1", "Example University")
    assert (scope.actor, scope.for_platform, scope.user_id) == (actor, True, "u-root")
    store.get_organization = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as refused:
        await pages.organization_of_path(
            "o2", _admin_id="root@elsewhere.org", actor=actor, op_store=store
        )
    assert refused.value.status_code == 404


def test_the_account_of_a_platform_administrator_is_shown_to_the_platform_alone():
    row = {
        "id": 1,
        "timestamp": datetime(2026, 11, 23, tzinfo=UTC),
        "action": "organization.member.remove",
        "actor_type": "admin",
        "actor_id": "u-root",
        "actor_email": "root@elsewhere.org",
    }

    for_the_organization = organizations.audit_entry_view(row)
    for_the_platform = organizations.audit_entry_view(row, for_platform=True)
    by_the_token = organizations.audit_entry_view(
        {**row, "actor_type": "admin_token", "actor_id": None, "actor_email": None},
        for_platform=True,
    )

    assert for_the_organization["actor"] == "platform_administrator"
    assert "actor_user_id" not in for_the_organization
    assert "actor_email" not in for_the_organization
    assert (for_the_platform["actor"], for_the_platform["actor_user_id"]) == (
        "platform_administrator",
        "u-root",
    )
    assert for_the_platform["actor_email"] == "root@elsewhere.org"
    # The admin token is no account.
    assert by_the_token["actor"] == "platform_administrator"
    assert "actor_user_id" not in by_the_token


# -- the list ----------------------------------------------------------------------


def _row(name: str, **figures) -> dict:
    return {
        "id": f"o-{name.lower()}",
        "name": name,
        "status": "active",
        "member_count": 0,
        "provider_ids": [],
        "requests": 0,
        "cost_usd": 0,
        "cost_amount": 0,
        "spent_month_usd": 0,
        "spent_month_amount": 0,
        **figures,
    }


def _store(*rows: dict) -> SimpleNamespace:
    return SimpleNamespace(
        list_organizations=AsyncMock(side_effect=lambda **_: [dict(row) for row in rows])
    )


def _names(view: dict) -> list[str]:
    return [row["name"] for row in view["organizations"]]


@pytest.mark.asyncio
async def test_a_list_of_no_organization_has_zero_totals_and_an_unknown_order_is_refused():
    store = _store()

    view = await organizations.organizations_view(store, "7d", search="   ")

    assert (view["organizations"], view["total"]) == ([], 0)
    assert view["totals"] == {
        "organizations": 0,
        "members": 0,
        "requests": 0,
        "cost_usd": 0.0,
        "cost_amount": Decimal(0),
    }
    assert view["status_counts"] == {"active": 0, "suspended": 0}
    with pytest.raises(ValueError, match="unknown organization order"):
        await organizations.organizations_view(store, "7d", sort_by="created")


@pytest.mark.asyncio
async def test_rows_with_one_figure_stay_in_the_order_of_their_names_both_ways():
    # The store gives Beta before alpha: the view puts the two in the order of their names.
    store = _store(
        _row("Delta", requests=4),
        _row("Beta", requests=1),
        _row("alpha", requests=1),
        _row("Gamma"),
    )

    up = await organizations.organizations_view(store, "7d", sort_by="requests")
    down = await organizations.organizations_view(store, "7d", sort_by="requests", descending=True)
    past_the_end = await organizations.organizations_view(store, "7d", limit=2, offset=9)
    from_the_third = await organizations.organizations_view(store, "7d", offset=2)

    # alpha and Beta have one figure: alpha comes first in both orders.
    assert _names(up) == ["Gamma", "alpha", "Beta", "Delta"]
    assert _names(down) == ["Delta", "alpha", "Beta", "Gamma"]
    assert (past_the_end["organizations"], past_the_end["total"]) == ([], 4)
    assert _names(from_the_third) == ["Delta", "Gamma"]
    # Nobody spent: each share is 0, and nothing is divided by it.
    assert {row["spend_share"] for row in up["organizations"]} == {0.0}


@pytest.mark.asyncio
async def test_the_share_and_the_order_follow_the_amount_in_the_ledger_currency():
    """On a ledger that is not in USD, a row can have a larger USD figure and a smaller amount."""
    store = _store(
        _row("Alpha", cost_usd=3, cost_amount=Decimal("10")),
        _row("Beta", cost_usd=2, cost_amount=Decimal("30")),
    )

    view = await organizations.organizations_view(store, "30d", sort_by="spend", descending=True)

    assert _names(view) == ["Beta", "Alpha"]
    assert [row["spend_share"] for row in view["organizations"]] == [0.75, 0.25]
    assert (view["totals"]["cost_usd"], view["totals"]["cost_amount"]) == (5.0, Decimal("40"))


@pytest.mark.asyncio
async def test_without_a_period_no_usage_is_asked_for_and_none_is_shown():
    plain = {
        key: value
        for key, value in _row("Alpha", member_count=3).items()
        if key not in ("requests", "cost_usd", "cost_amount")
    }
    store = _store(plain)

    view = await organizations.organizations_view(store)

    store.list_organizations.assert_awaited_once_with()
    [row] = view["organizations"]
    assert "spend_share" not in row and "requests" not in row
    assert (view["period"], view["totals"]) == (None, {"organizations": 1, "members": 3})
    for order in organizations.ORDERS_OF_A_PERIOD:
        with pytest.raises(ValueError, match="needs a period"):
            await organizations.organizations_view(store, sort_by=order)
    with_a_period = await organizations.organizations_view(_store(_row("Alpha")), "1d")
    assert with_a_period["period"] == "1d"


def test_the_route_takes_each_order_that_the_view_has():
    sort_by = inspect.signature(admin_routes.list_organizations).parameters["sort_by"]

    assert sort_by.annotation == "OrganizationOrder"
    assert set(get_args(admin_routes.OrganizationOrder)) == set(organizations.ORGANIZATION_ORDER)
    assert set(organizations.ORDERS_OF_A_PERIOD) < set(organizations.ORGANIZATION_ORDER)
