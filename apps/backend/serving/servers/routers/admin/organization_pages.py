"""One organization, for the platform administrator: the routes of the organization console.

Each route below ``/user/organization``
(:mod:`serving.servers.routers.organization`) is served a second time below
``/admin/organizations/{organization_id}``, by the same handler. One thing is
different: what tells the handler where it works. There it is the membership of
the caller. Here it is the organization of the path, for a platform
administrator (:func:`organization_of_path`). So the two sets of routes cannot
differ in a parameter, a rule or a body, and a route added to the organization
console is here too.

The route that reads the organization itself is not repeated:
``GET /admin/organizations/{organization_id}`` gives it
(:mod:`serving.servers.routers.admin.organizations`).

A handler knows who calls through ``OrganizationAdmin.for_platform``. Two use
it: the audit log shows a platform administrator the account of an actor that
is a platform administrator, and a removal tells the member who removed it.

A suspended organization is read and changed here as an active one: these
routes are the platform administrator's, whom no suspension refuses.
"""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin, get_type_hints

from fastapi import APIRouter, Depends, HTTPException

from serving.audit import AuditActor  # noqa: TC001 (FastAPI reads the annotation)
from serving.servers.deps import (
    OrganizationAdmin,
    get_admin_audit_actor,
    get_operational_store,
    require_organization_admin,
    verify_admin_access,
)
from serving.servers.routers import organization as console

if TYPE_CHECKING:
    from collections.abc import Callable

router = APIRouter(prefix="/admin/organizations/{organization_id}")


async def organization_of_path(
    organization_id: str,
    _admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationAdmin:
    """Admit a platform administrator to the organization that the path names.

    What a handler of the organization console is given in place of the
    membership of the caller. Each write it makes records the platform
    administrator as the actor, in the audit log of the organization.

    Raises:
        HTTPException: 404 for no such organization.
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    organization = await op_store.get_organization(organization_id)
    if organization is None:
        raise HTTPException(404, "No such organization.")
    return OrganizationAdmin(
        organization_id=organization["id"],
        organization_name=organization["name"],
        # The account, and not what ``verify_admin_access`` names the caller by.
        user_id=actor.id,
        actor=actor,
        for_platform=True,
    )


def _asks_for_the_membership(marker: Any) -> bool:
    return getattr(marker, "dependency", None) is require_organization_admin


def for_the_organization_of_the_path(endpoint: Callable[..., Any]) -> Callable[..., Any]:
    """A handler of the organization console, for the organization that the path names.

    The same function with one parameter changed: where it asks for
    ``require_organization_admin``, as a default or in ``Annotated``, it gets
    :func:`organization_of_path`.

    Raises:
        TypeError: The handler does not ask for ``require_organization_admin``:
            it would work in no organization here.
    """
    hints = get_type_hints(endpoint, include_extras=True)
    parameters, scoped = [], False
    for parameter in inspect.signature(endpoint).parameters.values():
        annotation = hints.get(parameter.name, parameter.annotation)
        default = parameter.default
        if _asks_for_the_membership(default):
            default, scoped = Depends(organization_of_path), True
        elif get_origin(annotation) is Annotated:
            kind, *markers = get_args(annotation)
            if any(_asks_for_the_membership(marker) for marker in markers):
                swapped = [
                    Depends(organization_of_path) if _asks_for_the_membership(marker) else marker
                    for marker in markers
                ]
                annotation, scoped = Annotated[(kind, *swapped)], True
        parameters.append(parameter.replace(annotation=annotation, default=default))
    if not scoped:
        raise TypeError(f"{endpoint.__name__} does not ask for require_organization_admin")

    @functools.wraps(endpoint)
    async def handler(*args: Any, **kwargs: Any) -> Any:
        return await endpoint(*args, **kwargs)

    # The signature FastAPI reads the parameters and their dependencies from.
    handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters, return_annotation=hints.get("return", inspect.Signature.empty)
    )
    return handler


#: What a route says of its answer and of its documentation, copied as it is.
_COPIED = (
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


def serve_for_the_platform(route: Any) -> None:
    """Serve one route of the organization console below the organization of the path.

    The dependencies this sees are those of the route and of the console's
    router. One given where the application includes that router is not seen.

    Raises:
        TypeError: The route has a dependency that is no parameter of its
            handler. It admitted a member of the organization there, and
            nothing says what it is for a platform administrator. Or its
            handler asks for no organization
            (:func:`for_the_organization_of_the_path`).
    """
    if route.dependencies:
        raise TypeError(f"{route.name} has dependencies of its route")
    router.add_api_route(
        route.path.removeprefix(console.router.prefix),
        for_the_organization_of_the_path(route.endpoint),
        methods=sorted(route.methods),
        **{name: getattr(route, name) for name in _COPIED},
    )


for _route in console.router.routes:
    # All but the organization itself: GET /admin/organizations/{organization_id}.
    if _route.path != console.router.prefix:  # type: ignore[attr-defined]
        serve_for_the_platform(_route)
