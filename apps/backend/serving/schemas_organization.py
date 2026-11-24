"""Request and response models of the organization routes.

Shared by the organization console (``/user/organization``) and the Admin
routes (``/admin/organizations``): an organization is shown in one shape to
its own administrators and to the platform administrator.

No model here has a field for request content or for the value of an API key:
an organization administrator sees how much its members use, never what they
send.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator

from serving.accounting_api import MoneyInput, MoneyResponse

#: The most a monthly limit can be: what ``DECIMAL(14, 4)`` holds for the
#: organization, and ``DECIMAL(10, 4)`` for one member.
ORGANIZATION_LIMIT_MAX_USD = Decimal("9999999999.9999")
MEMBER_LIMIT_MAX_USD = Decimal("999999.9999")

Period = Literal["1d", "7d", "30d"]
MemberRole = Literal["member", "admin"]
OrganizationStatus = Literal["active", "suspended"]


def _without_outer_spaces(value: Any) -> Any:
    """A name as it is stored; a name of spaces alone is then too short to be one."""
    return value.strip() if isinstance(value, str) else value


class OrganizationRef(BaseModel):
    """The organization an account belongs to, on the account's own profile."""

    id: str
    name: str
    role: MemberRole


class AccountOrganization(OrganizationRef):
    """The organization of an account, as the platform administrator is shown it.

    With the status of the organization: an account of a suspended organization
    is active itself, and the gateway refuses it.
    """

    status: OrganizationStatus = "active"


class OrganizationConnection(BaseModel):
    """One SSO connection of an organization, without its secret or client id.

    ``available`` is False when ``SSO_PROVIDERS`` has no entry with this id any
    more; every other field is then unset.
    """

    provider_id: str
    available: bool
    display_name: str | None = None
    type: str | None = None
    domains: list[str] = Field(default_factory=list)
    enabled: bool = False
    auto_create: bool = False
    require_approval: bool = False


class OrganizationAdministrator(BaseModel):
    """A member that has the administrator role."""

    user_id: str
    email: str | None = None
    user_name: str | None = None
    last_login_at: datetime | None = None


class OrganizationResponse(MoneyResponse):
    """One organization: the record, its connections, its administrators and its limits."""

    id: str
    name: str
    #: ``suspended`` while the gateway refuses the members of the organization.
    status: OrganizationStatus = "active"
    created_at: datetime
    member_count: int
    #: NULL is no limit.
    monthly_limit_usd: float | None = None
    monthly_limit: Decimal | None = None
    #: What a member gets that the organization set no limit for; NULL is none.
    #: Not an administrator, of the organization or of the platform.
    default_member_monthly_limit_usd: float | None = None
    default_member_monthly_limit: Decimal | None = None
    #: What the organization's counter holds for this UTC month.
    spent_month_usd: float = 0.0
    spent_month: Decimal | None = None
    connections: list[OrganizationConnection] = Field(default_factory=list)
    administrators: list[OrganizationAdministrator] = Field(default_factory=list)


class OrganizationListItem(OrganizationResponse):
    """One organization in the list of the platform administrator.

    The usage is that of the period the list was asked for, and null in a list
    that was asked for no period.
    """

    #: The email domains of its SSO connections.
    domains: list[str] = Field(default_factory=list)
    #: The logged requests of its members in the period, free and failed ones too.
    requests: int | None = None
    #: What its members spent in the period.
    cost_usd: float | None = None
    cost: Decimal | None = None
    #: Its part of what all organizations spent in the period, from 0 to 1.
    spend_share: float | None = None


class OrganizationTotals(MoneyResponse):
    """The figures over all organizations, whatever the list is narrowed to.

    The requests and the spend are those of the period, and null in a list
    that was asked for no period.
    """

    organizations: int = 0
    members: int = 0
    requests: int | None = None
    cost_usd: float | None = None
    cost: Decimal | None = None


class OrganizationStatusCounts(BaseModel):
    """How many organizations have each status, whatever the list is narrowed to."""

    active: int = 0
    suspended: int = 0


class AvailableConnection(BaseModel):
    """An ``SSO_PROVIDERS`` entry that belongs to no organization, and can be attached to one."""

    provider_id: str
    display_name: str | None = None
    domains: list[str] = Field(default_factory=list)
    enabled: bool = False


class ListOrganizationsResponse(BaseModel):
    """The organizations of the deployment, with the usage of a period when one was asked for.

    ``total`` is the number of organizations after ``status`` and ``search``.
    ``totals`` and ``status_counts`` are over all organizations.
    """

    organizations: list[OrganizationListItem]
    total: int = 0
    period: Period | None = None
    totals: OrganizationTotals = Field(default_factory=OrganizationTotals)
    status_counts: OrganizationStatusCounts = Field(default_factory=OrganizationStatusCounts)
    available_connections: list[AvailableConnection] = Field(default_factory=list)


class CreateOrganizationRequest(MoneyInput):
    """Make an organization from one SSO connection or more.

    The monthly limit is either the USD ``monthly_limit_usd`` or the native
    ``monthly_limit`` with the accounting ``currency``, never both.
    """

    usd_places: ClassVar[dict[str, int]] = {"monthly_limit_usd": 4}

    name: str = Field(..., min_length=1, max_length=80)
    provider_ids: list[str] = Field(..., min_length=1, max_length=20)

    _name = field_validator("name", mode="before")(_without_outer_spaces)
    monthly_limit_usd: Decimal | None = Field(
        default=None, ge=0, le=ORGANIZATION_LIMIT_MAX_USD, decimal_places=4
    )
    monthly_limit: Decimal | None = Field(default=None, ge=0, exclude=True)


class UpdateOrganizationRequest(MoneyInput):
    """Change the name or the monthly limit; a field that is not sent stays as it is.

    ``null`` for the monthly limit clears it.
    """

    usd_places: ClassVar[dict[str, int]] = {"monthly_limit_usd": 4}

    name: str | None = Field(default=None, min_length=1, max_length=80)
    monthly_limit_usd: Decimal | None = Field(
        default=None, ge=0, le=ORGANIZATION_LIMIT_MAX_USD, decimal_places=4
    )
    monthly_limit: Decimal | None = Field(default=None, ge=0, exclude=True)

    _name = field_validator("name", mode="before")(_without_outer_spaces)


class OrganizationCreatedResponse(BaseModel):
    """An organization that was just made, and how many accounts became its members."""

    organization: OrganizationResponse
    members_added: int


class ConnectionAttachedResponse(BaseModel):
    """An attached SSO connection, and how many accounts became members with it."""

    organization: OrganizationResponse
    members_added: int


class SetMemberRoleRequest(BaseModel):
    """Give a member the administrator role, or take it back."""

    role: MemberRole
    password: str | None = Field(
        None,
        description="The signed-in administrator's password: needed to give the role",
    )


class SetMonthlyLimitRequest(MoneyInput):
    """A monthly limit, or ``null`` for none.

    Either the USD ``monthly_limit_usd`` or the native ``monthly_limit`` with
    the accounting ``currency``, never both.
    """

    usd_places: ClassVar[dict[str, int]] = {"monthly_limit_usd": 4}

    monthly_limit_usd: Decimal | None = Field(
        default=None, ge=0, le=MEMBER_LIMIT_MAX_USD, decimal_places=4
    )
    monthly_limit: Decimal | None = Field(default=None, ge=0, exclude=True)


class DeleteOrganizationRequest(BaseModel):
    """Delete an organization. This cannot be undone."""

    confirm_name: str = Field(
        ...,
        max_length=200,
        description="The name of the organization, exactly: a wrong id must not delete one",
    )
    password: str | None = Field(
        None,
        description="The signed-in administrator's password, for an account that confirms with it",
    )
    disable_connections: bool = Field(
        True,
        description=(
            "Switch off the SSO_PROVIDERS entries of the organization, so that a first "
            "sign-in through one makes no personal account"
        ),
    )


class OrganizationDeletedResponse(BaseModel):
    """What a delete did."""

    message: str
    #: The member accounts that were active and are now suspended.
    accounts_suspended: int
    #: The ``SSO_PROVIDERS`` entries this delete switched off.
    connections_disabled: list[str] = Field(default_factory=list)


class OrganizationMessageResponse(BaseModel):
    """The outcome of an action that returns nothing else."""

    message: str


class SpendDay(MoneyResponse):
    """What a member spent on one UTC day (``YYYY-MM-DD``)."""

    day: str
    cost_usd: float = 0.0
    cost: Decimal | None = None


class OrganizationMember(MoneyResponse):
    """One member, with its usage in the requested period.

    The requests, the tokens and ``cost`` are those of the period. The four
    ``spent_*`` figures are the same for every period: today, the last 7 days,
    this month and all time. They are of the whole account, the figures that
    Admin has for it.

    ``status`` is the account's status. ``removed`` is True for a member the
    organization removed: only such a member is the organization's to restore.
    ``protected`` is True for an administrator, of the organization or of the
    platform: the organization sets no limit of it and does not remove it.

    Two parties can limit a member, and the lower limit applies:

    - its organization, with the limit it set for the member
      (``own_monthly_limit``), else with its default;
    - the platform administrator, with the monthly quota of the account
      (``platform_monthly_limit``), which the organization can read and not
      change.

    ``monthly_limit`` is the limit that applies, and ``monthly_limit_source``
    says which one it is: ``member``, ``default``, ``platform``, or ``none``
    for no limit.
    """

    user_id: str
    email: str | None = None
    user_name: str | None = None
    role: MemberRole
    protected: bool = False
    status: str
    removed: bool = False
    joined_at: datetime
    last_login_at: datetime | None = None
    last_request_at: datetime | None = None
    requests: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost: Decimal | None = None
    spent_month_usd: float = 0.0
    spent_month: Decimal | None = None
    spent_today_usd: float = 0.0
    spent_today: Decimal | None = None
    spent_7d_usd: float = 0.0
    spent_7d: Decimal | None = None
    spent_alltime_usd: float = 0.0
    spent_alltime: Decimal | None = None
    #: The spend of each of the last 7 UTC days, the oldest first, with today.
    spend_days: list[SpendDay] = Field(default_factory=list)
    monthly_limit_usd: float | None = None
    monthly_limit: Decimal | None = None
    monthly_limit_source: Literal["member", "default", "platform", "none"] = "none"
    own_monthly_limit_usd: float | None = None
    own_monthly_limit: Decimal | None = None
    platform_monthly_limit_usd: float | None = None
    platform_monthly_limit: Decimal | None = None


class ListMembersResponse(BaseModel):
    """One page of members and how many match the filters."""

    total: int
    members: list[OrganizationMember]


class OrganizationAuditEntry(BaseModel):
    """One management action of an organization.

    ``actor`` is ``platform_administrator`` for a platform administrator,
    whose account an organization is not shown; ``member`` otherwise, with the
    account in ``actor_user_id`` and ``actor_email``. A platform administrator
    who reads the log is shown the account of a platform administrator too.
    """

    id: int
    timestamp: datetime
    action: str
    category: Literal["membership", "limits", "access", "organization"]
    actor: Literal["platform_administrator", "member", "system"]
    actor_user_id: str | None = None
    actor_email: str | None = None
    target_type: str | None = None
    target_user_id: str | None = None
    target_email: str | None = None
    details: dict | None = None


class ListOrganizationAuditResponse(BaseModel):
    """One page of an organization's audit log, newest first."""

    total: int
    entries: list[OrganizationAuditEntry]


class UsagePoint(MoneyResponse):
    """One point of a usage series: a UTC day, or an hour for the ``1d`` period."""

    bucket: datetime
    cost_usd: float = 0.0
    cost: Decimal | None = None
    requests: int = 0
    total_tokens: int = 0
    members_with_requests: int = 0


class ModelUsage(MoneyResponse):
    """What one model was used for in a period."""

    model_id: str
    requests: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: The input tokens that the provider read from its cache.
    cached_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost: Decimal | None = None


class PeriodTotals(MoneyResponse):
    """Spend and requests of a period, for the change against the period before."""

    cost_usd: float = 0.0
    cost: Decimal | None = None
    requests: int = 0


class OrganizationOverviewResponse(MoneyResponse):
    """The organization's usage in a period.

    ``previous`` is the period of the same length before this one, for ``1d``
    and ``7d``. ``spent_month_usd`` is the organization's counter for this UTC
    month, the amount its limit is checked against.
    """

    period: Period
    bucket: Literal["day", "hour"]
    cost_usd: float = 0.0
    cost: Decimal | None = None
    requests: int = 0
    total_tokens: int = 0
    members_with_requests: int = 0
    member_count: int = 0
    previous: PeriodTotals | None = None
    spent_month_usd: float = 0.0
    spent_month: Decimal | None = None
    monthly_limit_usd: float | None = None
    monthly_limit: Decimal | None = None
    series: list[UsagePoint] = Field(default_factory=list)
    models: list[ModelUsage] = Field(default_factory=list)
    #: The members with the highest spend in the period, the highest first.
    top_members: list[OrganizationMember] = Field(default_factory=list)


class RequestVolumeWindow(BaseModel):
    """The requests of one window that ends now.

    A request is ``failed`` with a status of 400 or more. ``avg_latency_ms`` is
    None when no request of the window recorded a latency.
    """

    requests: int = 0
    ok: int = 0
    failed: int = 0
    avg_latency_ms: float | None = None


class RequestVolumeResponse(BaseModel):
    """The requests of the members in the last hour, day, week and month (30 days)."""

    hour: RequestVolumeWindow
    day: RequestVolumeWindow
    week: RequestVolumeWindow
    month: RequestVolumeWindow


class OrganizationRequest(MoneyResponse):
    """One request of a member: who, with what, how much. Never its content."""

    id: int
    timestamp: datetime
    user_id: str
    email: str | None = None
    user_name: str | None = None
    project_name: str | None = None
    key_name: str | None = None
    model_id: str
    status_code: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cost: Decimal | None = None


class ListOrganizationRequestsResponse(BaseModel):
    """One page of requests, newest first; ``next_before`` asks for the page after it."""

    requests: list[OrganizationRequest]
    next_before: str | None = None


class ModelUsageResponse(BaseModel):
    """The models the organization's members used in a period."""

    period: Period
    models: list[ModelUsage]


class MemberSummaryCard(BaseModel):
    """How many members a card stands for, and the first of them."""

    count: int
    top: list[OrganizationMember]


class MembersSummaryResponse(BaseModel):
    """The two cards above the member list.

    ``near_limit``: the members whose spend of this month is 80% or more of
    the monthly limit that applies to them, the highest share first.
    ``top_spenders_today``: the members that spent anything today, the highest
    spend first.
    """

    near_limit: MemberSummaryCard
    top_spenders_today: MemberSummaryCard


class MemberAccount(BaseModel):
    """What the account of a member has: an active API key, a second factor, a verified email."""

    has_key: bool = False
    two_factor_enabled: bool = False
    email_verified: bool = False


class MemberProject(MoneyResponse):
    """A project of a member: its name and what it spent, never its budgets or its models."""

    id: str
    name: str
    is_default: bool = False
    created_at: datetime
    spent_today_usd: float = 0.0
    spent_today: Decimal | None = None
    spent_month_usd: float = 0.0
    spent_month: Decimal | None = None


class MemberKey(MoneyResponse):
    """An API key of a member by its name: never its value, its prefix or its limits.

    ``id`` names the key to :func:`revoke <serving.servers.routers.organization.revoke_member_key>`.
    """

    id: int
    name: str | None = None
    status: str
    project_id: str | None = None
    project_name: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    spent_today_usd: float = 0.0
    spent_today: Decimal | None = None
    spent_month_usd: float = 0.0
    spent_month: Decimal | None = None


class MemberDetailResponse(BaseModel):
    """One member: its row, its spend for each of the last 30 days, its models, its last requests.

    ``disabled_models`` are the models the organization turned off for the
    member; ``platform_disabled_models`` those the platform administrator
    turned off, which the organization can read and not change. A model in
    either is refused.
    """

    member: OrganizationMember
    account: MemberAccount = Field(default_factory=MemberAccount)
    days: list[UsagePoint]
    models: list[ModelUsage]
    requests: list[OrganizationRequest]
    projects: list[MemberProject] = Field(default_factory=list)
    keys: list[MemberKey] = Field(default_factory=list)
    disabled_models: list[str] = Field(default_factory=list)
    platform_disabled_models: list[str] = Field(default_factory=list)


class SetMemberModelsRequest(BaseModel):
    """The models an organization turns off for one member; an empty list turns none off."""

    disabled_models: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        max_length=500
    )


class MemberModelsResponse(BaseModel):
    """The models of one member, as its organization sets them.

    ``available`` are the canonical ids of the models the platform lets the
    member use, the ones an organization can turn off.
    """

    available: list[str]
    disabled_models: list[str]
    platform_disabled_models: list[str]
