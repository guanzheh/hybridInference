"""Every route authenticates where the status of the caller's organization is read.

A suspended organization refuses each request of a member, not only the ones
that cost money. So the check is not in the spend check: it is where the
gateway decides who the caller is. That holds only while every route comes
through one of those places. This walks the application, as the body limit
test does, and fails for a route that does neither of two things:

- depend on a function that is listed here, with what it does about a
  suspended organization; or
- be named in :data:`NAMED`, with the reason it needs no such dependency.

A new route that takes an account in another way must be added here on
purpose, which is the moment to give it the check.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute, _IncludedRouter

from serving.servers import auth, deps
from serving.servers.routers import (
    agent_grants,
    alert_history_auth,
    auth_routes,
    billing,
    handoff,
    identity,
    internal_auth,
    internal_lookups,
    phone_auth_routes,
    qdrant_proxy,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

#: Dependencies that refuse a member of a suspended organization.
REFUSE = {
    auth.verify_api_key: "the key lookup, and the account a request is charged to",
    auth.verify_batch_management_identity: "the key lookup",
    auth.optional_verify_api_key: "the lightweight key lookup",
    auth.verify_api_key_for_balance: "the key lookup",
    deps.get_current_user: "the reader for one account",
}

#: Dependencies that call one of :data:`REFUSE` themselves, with the name they call.
WRAP = {
    qdrant_proxy._verify_qdrant_user: "verify_api_key",
    billing.get_billing_snapshot: "_load_snapshot",
    billing.get_quota_snapshot: "_load_snapshot",
}

#: Dependencies that admit no member of an organization, or must stay open for one.
OTHER = {
    deps.verify_admin_access: "the platform administrator, whom no suspension refuses",
    deps.get_current_user_to_sign_out: "sign-out: a member must be able to end its session",
    internal_auth.require_dispatch_token: (
        "the dispatch token of the control plane; a handler that answers for an account "
        "asks serving.organizations.account_available"
    ),
    alert_history_auth.require_history_reader: "the platform administrator or a reader token",
    alert_history_auth.require_history_writer: "a writer token of the deployment",
    handoff.require_release: "the release token of the deployment",
}

_PUBLIC = "public: it authenticates nobody"
_BEFORE_A_SESSION = "it starts a sign-in, a signup or a recovery: no account is admitted yet"
_SIGN_IN = "it makes or extends a session: its handler checks the organization ({how})"
_NO_MEMBER_SIGNS_IN = (
    "it signs no member in that a suspension applies to: such an account signs in "
    "through single sign-on only (signs_in_through_sso_only)"
)
_BROWSER = "the accounts of a browser: it lists them, signs them out or drops a link; no token"
_RETIRED = "retired: a static 410 for everyone"
_ADMINISTRATOR = "the platform administrator, by a check in the handler"

#: Routes with none of the dependencies above: ``(method, path)`` to the reason.
NAMED: dict[tuple[str, str], str] = {
    **dict.fromkeys(
        [
            ("GET", "/"),
            ("GET", "/health"),
            ("GET", "/health/deep"),
            ("GET", "/health/ready"),
            ("GET", "/public-stats"),
            ("GET", "/routing"),
            ("GET", "/site-config"),
            ("GET", "/site-updates"),
            ("GET", "/v1/identity/jwks"),
            ("GET", "/auth/setup/status"),
        ],
        _PUBLIC,
    ),
    **dict.fromkeys(
        [
            ("POST", "/auth/setup/admin"),
            ("POST", "/auth/signup"),
            ("POST", "/auth/phone/signup"),
            ("POST", "/auth/phone/code"),
            ("POST", "/auth/forgot-password"),
            ("POST", "/auth/reset-password"),
            ("POST", "/auth/resend-verification"),
            ("GET", "/auth/verify-email"),
            ("POST", "/auth/sso/discover"),
            ("GET", "/auth/sso/{provider_id}/start"),
        ],
        _BEFORE_A_SESSION,
    ),
    ("POST", "/auth/login"): _NO_MEMBER_SIGNS_IN,
    ("POST", "/auth/phone/login"): _NO_MEMBER_SIGNS_IN,
    ("GET", "/auth/sso/{provider_id}/callback"): _SIGN_IN.format(
        how="sso_accounts.resolve_sign_in, and the store for a first sign-in or a link"
    ),
    ("POST", "/auth/refresh"): _SIGN_IN.format(how="_usable_account"),
    ("POST", "/auth/switch"): _SIGN_IN.format(how="_usable_account"),
    ("POST", "/v1/identity/token"): _SIGN_IN.format(how="organizations.account_available"),
    **dict.fromkeys(
        [
            ("GET", "/auth/accounts"),
            ("DELETE", "/auth/accounts/{user_id}"),
            ("POST", "/auth/logout-all"),
            ("GET", "/auth/sso/link"),
            ("POST", "/auth/sso/link/cancel"),
        ],
        _BROWSER,
    ),
    **{
        (method, f"{prefix}/v1/messages/batches{path}"): _RETIRED
        for prefix in ("", "/anthropic")
        for method, path in (
            ("POST", ""),
            ("GET", ""),
            ("GET", "/{batch_id}"),
            ("POST", "/{batch_id}/cancel"),
            ("DELETE", "/{batch_id}"),
            ("GET", "/{batch_id}/results"),
        )
    },
    ("GET", "/admin/export/requests/download"): _ADMINISTRATOR,
    ("GET", "/internal/verify-admin"): _ADMINISTRATOR,
}


def _routes(entries: Iterable[Any]) -> Iterable[Any]:
    for entry in entries:
        if isinstance(entry, _IncludedRouter):
            yield from entry.effective_route_contexts()
        elif isinstance(entry, APIRoute):
            yield entry


def _dependencies(route: Any) -> set[Callable[..., Any]]:
    return {dependency.call for dependency in get_flat_dependant(route.dependant).dependencies}


def unclassified(app: FastAPI) -> list[str]:
    """The routes of *app* that are covered neither by a dependency nor by name."""
    known = REFUSE.keys() | WRAP.keys() | OTHER.keys()
    missing = []
    for route in _routes(app.routes):
        if _dependencies(route) & known:
            continue
        missing.extend(
            f"{method} {route.path}"
            for method in sorted(route.methods or ())
            if (method, route.path) not in NAMED
        )
    return sorted(missing)


def test_every_route_of_the_application_is_covered() -> None:
    from serving.servers.app import create_app

    assert unclassified(create_app()) == []


def test_no_named_route_is_left_over() -> None:
    """A name with no route, or with a route that a dependency covers, says nothing true."""
    from serving.servers.app import create_app

    known = REFUSE.keys() | WRAP.keys() | OTHER.keys()
    uncovered = {
        (method, route.path)
        for route in _routes(create_app().routes)
        if not _dependencies(route) & known
        for method in route.methods or ()
    }

    assert sorted(set(NAMED) - uncovered) == []


def test_a_route_that_is_covered_by_nothing_is_found() -> None:
    """The case this test exists for: a new route that takes an account in a way of its own."""

    def its_own_authentication() -> dict[str, str]:
        return {"user_id": "u1"}

    router = APIRouter()

    @router.get("/v1/new-surface")
    async def new_surface(user: dict = Depends(its_own_authentication)) -> dict:
        return user

    @router.post("/v1/covered")
    async def covered(user: dict = Depends(auth.verify_api_key)) -> dict:
        return user

    app = FastAPI()
    app.include_router(router)

    assert unclassified(app) == ["GET /v1/new-surface"]


def test_a_wrapper_calls_what_it_wraps() -> None:
    for wrapper, called in WRAP.items():
        assert f"{called}(" in inspect.getsource(wrapper), wrapper.__qualname__
    # The billing snapshot reads the limits through the balance reader, which
    # authenticates the key as every other key route does.
    assert "read_balance_limits(" in inspect.getsource(billing._load_snapshot)
    assert "_authenticate_by_api_key(" in inspect.getsource(auth.read_balance_limits)


def test_each_place_that_refuses_reads_the_organization() -> None:
    """What each listed dependency rests on, so that the list cannot name one that reads nothing."""
    by_key = inspect.getsource(auth._authenticate_by_api_key)
    assert "_refuse_suspended_organization(request, user)" in by_key
    for dependency in (
        auth.verify_api_key,
        auth.verify_batch_management_identity,
        auth.verify_api_key_for_balance,
    ):
        assert "_authenticate_by_api_key(" in inspect.getsource(dependency), dependency.__name__
    # A request on behalf of an account: the account that is charged.
    assert "_refuse_suspended_organization(request, identity)" in inspect.getsource(
        auth.verify_api_key
    )
    assert "_refuse_suspended_organization(request, row)" in inspect.getsource(
        auth.optional_verify_api_key
    )
    assert "refuse_suspended_organization(" in inspect.getsource(deps._session_account)
    assert "check_organization=True" in inspect.getsource(deps.get_current_user)
    assert "check_organization=False" in inspect.getsource(deps.get_current_user_to_sign_out)


def test_the_password_and_the_phone_sign_in_refuse_each_account_of_single_sign_on_only() -> None:
    """The two sign-in handlers that read no status of an organization, and what stops a member.

    A suspension applies to the members that are not platform administrators,
    which are the accounts that sign in through single sign-on only
    (``storage.organizations.not_platform_administrator``, one definition for
    both rules). So these handlers must keep this refusal: without it, a member
    of a suspended organization would get a session here.
    """
    for handler in (auth_routes.login, phone_auth_routes.phone_login):
        assert "organizations.signs_in_through_sso_only(" in inspect.getsource(handler), (
            handler.__name__
        )
    # The other handlers that make or extend a session read the organization.
    assert "suspended_organization_of(" in inspect.getsource(auth_routes._usable_account)


def test_each_handler_that_answers_for_an_account_by_its_id_asks_if_it_is_available() -> None:
    """The routes behind a service token, and the exchange of an identity code."""
    for handler in (
        internal_lookups.model_catalog,
        internal_lookups.user_status,
        internal_lookups.user_agent_access,
        agent_grants._active_user,
        identity.exchange_authorization_code,
    ):
        assert "organizations.account_available(" in inspect.getsource(handler), handler.__name__
    # The mint and the renewal of a grant check the account through one function.
    for handler in (agent_grants.mint_grant, agent_grants.renew_grant):
        assert "_active_user(" in inspect.getsource(handler), handler.__name__
