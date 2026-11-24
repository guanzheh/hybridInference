# Organizations

This page is for an operator whose gateway serves the members of a university
or a company through [single sign-on](sso.md), and who wants that organization
to see and manage its own members. After reading it you can make an
organization, name its administrators and set its limits, and you know what
its administrators see and can change. You can also suspend an organization,
restore it and delete it.

A deployment that has no organization behaves as it did before, with one
exception: an account can connect an organization account under
**Settings → Single sign-on** only when its own email is in the `domains` of
the provider. [Single Sign-On](sso.md) describes this rule.

## What an organization is

An organization is a record of its own in the gateway. It owns one or more SSO
connections and the accounts of its members. An SSO connection is one entry of
`SSO_PROVIDERS`, the provider that [Single Sign-On](sso.md) describes.

- **A member** is an account that has an email in the `domains` of an SSO
  connection of the organization, and an organization account of that
  connection linked to it. An account becomes a member at its first sign-in
  through the connection, when its owner connects the organization account
  under **Settings → Single sign-on**, or when you attach the connection to
  the organization.
- **An organization administrator** is a member that you give that role. It
  sees the usage of the members and manages them. The role gives no access to
  **Admin**, and the `admin` role gives no role in an organization.
- **An account is in one organization at most, and so is an SSO connection.**
  A person who also wants a personal account registers a second account with
  another email.

A membership ends when the account is deleted permanently, or when you delete
the organization. The members stay when you detach an SSO connection, and when
the email of an account changes later.

### How a member signs in

A member signs in through single sign-on only. The organization owns the
account, so its identity provider decides who can sign in.

- When an account becomes a member, the gateway removes its password and its
  password reset links. This includes the accounts that become members when
  you attach a connection. They sign in through the connection from then on.
- A member cannot set a password, in **Settings** or with a reset link, and a
  phone code does not sign it in.
- An account with the `admin` role, or with an address in `ADMIN_EMAILS`, is
  the exception. It keeps its password, so that it can sign in when the
  identity provider is not available.
- An API key is not a sign-in. The keys of a member work until the member is
  removed or a key is revoked.
- `python -m serving.auth.reset_password` refuses a member too.

A person who registered with a password and connected the organization account
later is told about this:

- Before the connection, the console says that the account will sign in
  through the organization only and that its password is removed.
- After a failed password sign-in with an address of the organization, the
  sign-in page says that the accounts of the organization must sign in through
  single sign-on, and offers that sign-in. The page says this for each address
  of the domain, also for one that has no account.
- A member who asks for a reset link gets an email that says how the account
  signs in, in place of the link.

This is not undone. A member stays a member when you detach the connection,
and its account stays without a password. Check the `domains` of a connection
before you attach it.

Attach each SSO connection that answers for the email domains of an
organization. An organization account of a connection that is not attached
can also sign a member in.

## Set an organization up

The Admin console has no pages for these steps yet. Call the Admin API. Each
call takes `Authorization: Bearer <token>`. The token is `ADMIN_TOKEN`, or the
access token of an administrator's console session, which `POST /auth/login`
returns as `access_token`.

1. **Connect the identity provider** as [Single Sign-On](sso.md) describes,
   and note the `id` of its entry in `SSO_PROVIDERS`.
2. **Make the organization** from that SSO connection:

   ```bash
   curl -s https://your-gateway.example/admin/organizations \
     -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"name": "Example University", "provider_ids": ["example-univ"], "monthly_limit_usd": "1000"}'
   ```

   The answer holds the `id` of the organization, and `members_added`: the
   number of accounts that had signed in through the connection and are now
   members. Leave `monthly_limit_usd` out for an organization without a limit.
3. **Give the first administrator the role.** The person signs in through the
   SSO connection once, which makes the account a member. Find the account in
   **Admin → Users** and copy its user id. Then:

   ```bash
   curl -s -X PUT \
     "https://your-gateway.example/admin/organizations/$ORGANIZATION_ID/members/$USER_ID/role" \
     -H "Authorization: Bearer $ACCESS_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"role": "admin", "password": "<your password>"}'
   ```

   This call asks you to confirm who you are, as raising the role of an
   account does in **Admin → Users**. Send it with the access token of your
   own console session: `ADMIN_TOKEN` cannot give the role. An account with a
   password sends it in `password`. An account with an authenticator app gives
   a code in that session first, and needs no `password`. `{"role": "member"}`
   takes the role back and asks for no confirmation.

The other calls:

| Call | What it does |
|---|---|
| `GET /admin/organizations` | Lists the organizations: name, number of members, SSO connections, administrators, limits and the spend of this month |
| `GET /admin/organizations/{id}` | Shows one organization |
| `PATCH /admin/organizations/{id}` | Changes `name` or `monthly_limit_usd`. `null` clears the limit |
| `PUT /admin/organizations/{id}/connections/{provider_id}` | Attaches an SSO connection. Each account that matches the member rule and is in no organization becomes a member |
| `DELETE /admin/organizations/{id}/connections/{provider_id}` | Detaches an SSO connection. The members stay. New accounts of the connection do not become members |
| `POST /admin/organizations/{id}/suspend` | Suspends the organization: the gateway refuses its members |
| `POST /admin/organizations/{id}/restore` | Restores a suspended organization |
| `DELETE /admin/organizations/{id}` | Deletes the organization. This cannot be undone |

[Suspend, restore and delete an organization](#suspend-restore-and-delete-an-organization)
describes the last three.

An organization stores the `id` of each of its SSO connections. If you change
the `id` of an entry in `SSO_PROVIDERS`, the organization loses that
connection and shows it as not available. The members stay. Attach the new
`id`.

## What an organization administrator sees

An organization administrator has an entry in the navigation of the console,
under **Organization**, with the name of the organization. The entry opens
the organization console. No other account sees the entry.

The pages of the organization console read the routes below
`/user/organization`. A script can call the same routes with the access token
of the administrator's console session. The organization is always the one of
the caller's membership: no call names it. Every other caller gets a 403.

| Page | Route | Shows |
|---|---|---|
| **Settings** | `GET /user/organization` | The organization: name, SSO connections, administrators, limits and the spend of this month |
| **Overview** | `GET /user/organization/overview` | Totals of the period: spend, requests, tokens and the number of members with requests. A series of each day. For one day, a series of each hour that has begun; `granularity=hour` gives the hours of 7 days too. For each model: requests, errors, input, output and cached tokens, and spend. The 5 members with the highest spend |
| **Members** | `GET /user/organization/members` | One row for each member: name, email, role, status, last request, and the spend of today, of the last 7 days, of this month with the monthly limit, and of all time. Takes `search`, `status`, `limit_state`, `min_cost_today`, `min_cost_month`, `sort_by`, `order`, `limit` and `offset` |
| **Members**, the two cards | `GET /user/organization/members/summary` | The members near or over their monthly limit, and the members with the highest spend today: the number, and the first 5 |
| **Members**, in the panel of a member | `GET /user/organization/members/{user_id}` | One member: the same figures, the spend of each of the last 30 days, the spend of each model, and the last requests. Whether the account has an API key, a second factor and a verified email. Its projects and its API keys by name, with their spend. The models that are off for it |
| **Usage**, the four cards | `GET /user/organization/usage/volume` | The requests of the last hour, day, week and month: how many were OK, how many failed, and the average latency |
| **Usage → Requests** | `GET /user/organization/usage/requests` | One row for each request, newest first: time, member, project, name of the API key, model, status code, tokens and cost. Takes `search`, `model`, `status`, `limit`, and `before` for the next page |
| **Usage → By model** | `GET /user/organization/usage/models` | Requests, errors, tokens and spend of each model |
| **Usage → By member** | `GET /user/organization/usage/members` | Requests, tokens and spend of each member |
| **Audit log** | `GET /user/organization/audit-log` | The management actions of this organization, each with its details: for a change, the old value and the new value. Takes `category`, `member`, `search`, `limit` and `offset` |

The routes that show figures take `period`: `1d`, `7d` or `30d`, the last 1, 7
or 30 days in UTC, with today. The audit log takes it too, and shows every row
without it. "This month" is the calendar month in UTC, the month that an
account's monthly quota uses.

The four spend columns of a member do not follow the period. They are the
columns of **Admin → Users**: today, the last 7 days, this month and all time.
`limit_state=near` keeps the members whose spend of this month is 80% or more
of their monthly limit, and `limit_state=over` those at 100% or more.
`min_cost_today` and `min_cost_month` are amounts in USD, or in the ledger
currency when the call names it with `currency`.

The figures of a member are of its whole account. They are the figures that
**Admin → Users** shows for that account. So an organization administrator
also sees what an account used before it became a member, for example an
account that existed when you attached the connection.

Money fields follow [Accounting Currency](accounting.md): the `*_usd` fields,
and the native amounts with `currency` on a ledger that is not in USD.

No route shows these:

- the content of a request: the prompt, the response, the tools or the error
  text;
- the value of an API key, or the prefix that begins it. A request row and the
  panel of a member show the name of a key only;
- the address that a request or a management action came from;
- an account that is not a member, or a different organization;
- the configuration of the deployment, or the client secret of an SSO
  connection.

## What an organization administrator can change

On **Members**, a click on a row opens a panel with the details of the member
and the actions on that member: **Set monthly limit**, **Models**, **Revoke**
for an API key, **Remove from organization** and **Restore member**.
**Settings** has the default monthly limit for a member. The actions call
these routes:

| Route | What it does |
|---|---|
| `PUT /user/organization/members/{user_id}/monthly-limit` | Sets the monthly limit of a member. `{"monthly_limit_usd": null}` clears it |
| `PUT /user/organization/default-member-limit` | Sets the default monthly limit for a member. `null` clears it |
| `POST /user/organization/members/{user_id}/remove` | Removes a member |
| `POST /user/organization/members/{user_id}/restore` | Restores a member that the organization removed |
| `PUT /user/organization/members/{user_id}/models` | Sets the models that are off for a member. `{"disabled_models": []}` turns none off |
| `POST /user/organization/members/{user_id}/keys/{key_id}/revoke` | Revokes one API key of a member |

The routes that name a member do not apply to an organization
administrator, to an account with the `admin` role, or to the caller. The
answer is a 409. The row of such a member has `protected: true`, and the
console shows no action in its panel. The default monthly limit does not apply
to these accounts either.

### Turn a model off, and revoke a key

An organization can only take away. It cannot give a member more than you
allow.

- **Models.** An organization administrator turns a model off for one member.
  The organization has a list of its own for this. The list that you set in
  **Admin → Users** is a different list: the organization can read it and
  cannot change it. The gateway refuses a model that is in either list. So an
  organization cannot turn on a model that you turned off.
  `GET /user/organization/members/{user_id}/models` returns the models that
  you let the member use, and the two lists. The organization can turn off
  only those models. Its list does not apply to an organization administrator
  or to an account with the `admin` role.
- **API keys.** An organization administrator revokes one API key of a member,
  as the owner of the key does in the console. The key stays revoked, and the
  member can make a new one. `key_id` is the `id` of the key in the panel of
  the member.

### Remove and restore a member

To remove a member is to suspend its account, as **Admin → Users** does:

- the account can no longer sign in, through single sign-on either;
- its sessions end and its API keys are revoked;
- its usage history stays.

To restore a member makes the account active again. The revoked API keys stay
revoked, and the member makes new ones.

An organization administrator restores only a member that the organization
removed. An account that you suspended in **Admin → Users** is yours to make
active again. When you make a removed member active again there, the
organization sees the member as active too.

An account cannot be suspended two times. To keep a removed member suspended
on your own authority, write a suspension message for it in **Admin → Users**.
The suspension is then yours, and the organization can no longer restore the
member.

The gateway is not told when the identity provider disables an account. The
person can then no longer sign in, because a member has no password. Until an
administrator removes the member, its open sessions and its API keys keep
working.

## Limits

A request of a member must fit under the limits of its API key, its project
and its account, as every request must, and then under the monthly limit of
its organization.

| Limit | Who sets it | Applies to |
|---|---|---|
| The monthly limit of the organization | You, with `monthly_limit_usd` | What all members are charged in a calendar month in UTC |
| The default monthly limit for a member | An organization administrator | Each member that the organization set no limit for. Not an organization administrator, and not an account with the `admin` role |
| The monthly limit of a member | An organization administrator | That member |
| The monthly quota of an account | You, in **Admin → Users** | That account, a member or not |

A member can thus have two monthly limits: the limit of its organization, and
the monthly quota that you set in **Admin → Users**. The gateway applies the
lower one. An organization administrator sees your quota in the row of the
member and cannot change it. So an organization can bring down what a member
may spend, and cannot raise it above your quota.

An organization administrator cannot set a limit of a member, or the default,
above the limit of the organization. The daily quota of an account does not
change: it comes from the role of the account or from **Admin → Users**.

When you lower the limit of the organization, the limits of its members and
the default stay as they are, also when they are now above it. A request must
be under each limit, so the lower limit decides.

A request that the limit of the organization refuses gets a 429 with `scope`
set to `organization`. The answer names the organization and says when the
month starts again. It contains no amount: the limit and the spend of an
organization are for its administrators to see, and each member can get this
answer.

```json
{
  "error": "Organization monthly limit exceeded",
  "code": "budget_exceeded",
  "scope": "organization",
  "window": "month",
  "organization": "Example University",
  "reset_at": "2026-12-01T00:00:00+00:00",
  "contact_email": "support@your-gateway.example",
  "message": "Organization 'Example University' reached its monthly limit. Contact an administrator of your organization.",
  "retry_after": 1123200
}
```

A member that reaches its own limit, the limit of its organization or your
quota, gets the refusal of an account, with `scope` set to `account`.

Things to know:

- **The spend of an organization starts at membership.** What an account was
  charged before it became a member does not count against the limit of the
  organization. The usage pages show it.
- **A change of a limit, of the role of a member or of its models applies
  within 60 seconds.** A revoked key stops within 30 seconds.
- **Requests that run at the same time** can take an organization a little
  past its limit, as they can for the quota of an account.
- **A batch** of a member waits while the organization is at its limit, as it
  does for the monthly quota of the account. It continues when the limit is
  raised or the next month starts.
- **A balance poll**, such as `GET /v1/usage`, reports the limits of the key,
  the project and the account. It does not report the limit of the
  organization.
- **On a ledger that is not in USD**, send a limit in the ledger currency as
  `monthly_limit` with `currency`. The offline currency migration converts the
  limits of the organizations that exist.

## The organization of an account in Admin

**Admin → Users** shows the organization of each account. `GET /admin/users`
has `organization` on each row: the id and the name of the organization, and
the role of the account there, or `null`. `organization_id=<id>` keeps the
members of one organization, and `organization_id=none` the accounts that have
none. `GET /admin/users/{user_id}/detail` has the same field.

## Suspend, restore and delete an organization

### Suspend and restore

To suspend an organization is to stop its members and to change nothing else:

```bash
curl -s -X POST \
  "https://your-gateway.example/admin/organizations/$ORGANIZATION_ID/suspend" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

While an organization is suspended, the gateway refuses each member:

- a request with an API key, on each route. A list of models, a file upload
  and a batch result cost nothing, and are refused too;
- a request that a service sends on behalf of a member, and a model call of
  the Cloud Agent for a member;
- each page of the console. A member can still sign out;
- a sign-in through single sign-on, which is the only way that a member
  signs in. A person who signs in through an SSO connection of the
  organization for the first time gets no account.

A request with an API key gets a 403 that names the organization:

```json
{
  "error": "Organization suspended",
  "code": "organization_suspended",
  "organization": "Example University",
  "contact_email": "support@your-gateway.example",
  "message": "Organization 'Example University' is suspended. Contact an administrator of your organization."
}
```

On `/v1/messages` and the routes below `/anthropic`, the answer has the
Anthropic error form, as each refusal there has. Its type is
`permission_error` and its message is `Organization suspended`.

The console and the sign-in page tell a member what they tell a suspended
account.

A suspension applies to each member, an organization administrator too. It
does not apply to a member that is a platform administrator: an account with
the `admin` role, or with an address in `ADMIN_EMAILS`. That account is yours,
and it must be able to sign in to restore the organization.

Only the status of the organization changes. The accounts, the API keys and
the sessions stay as they are. So `POST /admin/organizations/{id}/restore`
brings everything back: the same API keys work, and a session that was open
works again.

Things to know:

- **A request that runs** when you suspend the organization finishes. The next
  request is refused.
- **How fast.** The gateway process that handled your call refuses a member
  from the next request. A different process of the same deployment refuses a
  request with an API key from the next request too, but can take 30 seconds
  for `GET /v1/models` and 60 seconds for a session. A restore has the same
  delays.
- **A batch** of a member waits while the organization is suspended, and is
  not failed. It continues within 5 minutes after a restore. A batch whose
  deadline passes while it waits expires, as each paused batch does.
- **The status** of an organization is the field `status` of
  `GET /admin/organizations/{id}`: `active` or `suspended`.

### Delete

A delete cannot be undone. To stop an organization for some time, suspend it.

```bash
curl -s -X DELETE \
  "https://your-gateway.example/admin/organizations/$ORGANIZATION_ID" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"confirm_name": "Example University", "password": "<your password>"}'
```

`confirm_name` is the name of the organization, exactly. The call asks you to
confirm who you are, as the call that gives the administrator role does.
`ADMIN_TOKEN` cannot delete an organization.

A delete does this:

- It suspends the account of each member that is active. The sessions end, the
  API keys are revoked, and the suspension message names the organization. It
  does not suspend a platform administrator, or an account that waits for
  approval.
- It removes the organization with its memberships, its administrators, its
  limits, and the models that it turned off for a member.
- It switches the `SSO_PROVIDERS` entries of its SSO connections off
  (`"enabled": false`). If such an entry stays on and has `auto_create`, each
  person who signs in through it for the first time gets a personal account.
  Send `"disable_connections": false` to keep the entries as they are.

The accounts, their usage and the audit rows of the organization stay.

The answer has the number of accounts that were suspended, and the `id` of
each entry that was switched off. A refusal has a `code`:

| Status and code | Why |
|---|---|
| 409 `confirm_name_mismatch` | `confirm_name` is not the name of the organization |
| 409 `settings_invalid` | `SSO_PROVIDERS` is not valid, so its entries cannot be switched off. Correct it in **Admin → Settings**, or send `"disable_connections": false` |
| 409 `last_sign_in` | You sign in only through an SSO connection of this organization. Set a password first, or send `"disable_connections": false` |
| 409 `settings_changed` | The settings were saved while the delete ran. Nothing changed. Send the call again |
| 503 `settings_unavailable` | The gateway cannot read its settings. Nothing changed. Send the call again when the database is connected |
| `settings_refused` | The settings refused the change. The status and the message are those that the settings gave. Nothing changed |
| 409 `organization_changed` | The organization got a different name or one more SSO connection while the delete ran. Nothing was deleted. Read the organization, then send the call again |
| 500 `delete_incomplete` | The delete failed after it started to switch the entries off. The organization is still there. The message says if the entries are off, or if they can be off. Read `SSO_PROVIDERS` in **Admin → Settings**, then send the call again |

Things to know:

- **The entries are switched off before the organization is deleted**, because
  an entry can be switched on again and a delete cannot be undone. The two
  steps are not one transaction. If the second step fails, the organization is
  still there, and nobody can start a sign-in through its SSO connections.
  Send the call again, or switch the entries on in **Admin → Settings**.
- **An entry that is off stops each account that signs in through it**: an
  account that is in no organization and an account with the `admin` role too.
  An account that signed in only through it sets a password with **Forgot
  password**, which needs outgoing email.
- **A different gateway process** applies the change of `SSO_PROVIDERS` within
  10 seconds. Until then, a first sign-in on that process can still make a
  personal account.
- **To make an account active again**, use **Admin → Users**. The account is
  then in no organization, and its API keys stay revoked. It has no password,
  because a member has none. Its owner sets one with **Forgot password**,
  which needs outgoing email.
- **To use an SSO connection again**, attach it to an organization first, and
  switch its entry on in **Admin → Settings** after that. In the other order,
  an entry with `auto_create` makes personal accounts until it is attached.

## The audit log

Each action on this page writes a row to the audit log, with the organization
it belongs to. You read the rows of one organization with
`GET /admin/audit-log?organization_id=<id>`. An organization administrator
reads the rows of its own organization. A row of yours names its actor
`platform_administrator`, without an account id or an email.

| Action | Written when |
|---|---|
| `organization.create`, `organization.update` | You make an organization or change its name |
| `organization.connection.attach`, `organization.connection.detach` | You attach or detach an SSO connection. An attach writes one row with the number of new members |
| `organization.admin.add`, `organization.admin.remove` | You give the administrator role or take it back |
| `organization.limit.set` | You change the monthly limit of the organization |
| `organization.default_member_limit.set`, `organization.member_limit.set` | An organization administrator changes the default limit or the limit of a member |
| `organization.member.join` | An account becomes a member at a sign-in or a link |
| `organization.member.remove`, `organization.member.restore` | An organization administrator removes or restores a member |
| `organization.member_models.set` | An organization administrator changes the models that are off for a member |
| `organization.member_key.revoke` | An organization administrator revokes an API key of a member |
| `organization.suspend`, `organization.restore` | You suspend or restore an organization. A call that changes nothing writes no row |
| `organization.delete` | You delete an organization. The row has the number of members, the number of accounts that were suspended, and the SSO connections |

A delete that switches `SSO_PROVIDERS` entries off also writes a
`config.update` row, as a save in **Admin → Settings** does. That row belongs
to no organization. A delete that answered `delete_incomplete` can have
switched the entries off without this row. The setting then still shows who
changed it last.

## Stop using an organization

Take the administrator role back from each of its administrators and clear its
monthly limit. The members keep their accounts and sign in through single
sign-on as before. They stay members: their accounts have no password. To
stop the members too, suspend the organization or delete it.
