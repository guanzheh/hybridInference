# Platform administrator console: organization overview and management

- Date: 2026-11-21
- Status: proposed, awaiting review. Implemented by the pull requests in §16
- Issue: #1830 (RFC). The organization side is #1829
- Scope: the status of an organization (active or suspended) and the checks that enforce it; suspend,
  restore and delete; an **Organizations** tab in the Admin Console; the pages of one organization for the
  platform administrator; the status of the organization of an account in **Admin → Users**. Unchanged:
  the `/v1` request and response formats, the member rule, the limits, and the organization console of
  #1829
- Based on: `guanzheh/claude/org-backend` @ `38cac901` (#1840) and `guanzheh/claude/org-console` @
  `02ac77fc` (#1841). Both are open. This work needs both. The first text of this record was on
  `bf946e25` and `5c35136c`; §18 has what the commits after them changed here
- Related records: [organization admin console](2026-11-18-organization-admin-console-design.md) (the
  record of #1829; "the organization record" below), [enterprise single sign-on](2026-11-13-enterprise-sso-oidc-design.md),
  [admin second factor](2026-11-15-admin-two-factor-design.md),
  [versioned schema migrations](2026-11-02-versioned-schema-migrations-design.zh.md),
  [audit attribution](2026-10-25-audit-attribution-design.md)

## 1. Background and goals

The organization record gives organizations, the member rule, the limits and a console for organization
administrators. It also gives the Admin routes that set an organization up. It gives no Admin page: the
platform administrator calls the routes by hand. It gives no way to stop an organization.

Goals:

1. The platform administrator sees all organizations, with the usage of a period and the limit.
2. The platform administrator makes an organization and changes its SSO connections, its administrators and
   its limit in the console.
3. The platform administrator suspends an organization, restores it, and deletes it.
4. The platform administrator opens the five pages of each organization and does the member actions.
5. **Admin → Users** shows the organization of each account and filters by it. #1840 and #1841 give
   this now (§8). This work adds the status of the organization.

## 2. What exists and what this work adds

| Part | Today (#1840, #1841) | This work |
|---|---|---|
| Make, rename, set the limit, attach and detach a connection, give and remove the administrator role | Admin routes exist (`routers/admin/organizations.py`) | The console pages that call them. The routes do not change |
| List of organizations | `GET /admin/organizations`: each organization with the spend of this month. No figures for a period, no search, no sort, no pages. Three statements for each organization | Figures for a period, totals, status, search, sort, pages. A fixed number of statements (§6) |
| Status of an organization | No column, no route, no check | The column, three routes, and the checks at each place that authenticates an account (§4, §5) |
| The pages of one organization | For its administrators, below `/user/organization`. The organization comes from the membership of the caller | The same pages for the platform administrator, below `/admin/organizations/{id}` (§7) |
| The member who leaves | Built: to remove a member suspends the account (the organization record §7.3) | No change. The platform administrator can do the same action (§7) |
| **Admin → Users** | The field `organization`, the parameter `organization_id`, the column and the filter | The status of the organization in the field, and a mark in the column (§8) |
| How a member signs in | Through single sign-on only. The join removes the password (the organization record §4.4) | No change. So the checks of §4.5 are at the SSO callback and at the routes that extend a session |

Six statements of the RFC and of the organization record do not match this design. This record replaces
them:

- RFC: "This RFC adds no table. The migration of #1829 gives the status." The organization record has no
  status column, because nothing read one. This work adds the column in a new revision (§11). It adds no
  table.
- RFC: "The platform administrator is not a member of an organization." An account with the `admin` role
  can match the member rule and is then a member. §4.2 says what applies to such an account.
- RFC: "Totals for a date range." The console has no date range control. The list takes the periods of the
  organization console: `1d`, `7d`, `30d`.
- The organization record §4.2: "A membership ends when the account is deleted permanently. Nothing else
  ends it." A delete of the organization also ends its memberships (§5).
- RFC: after a delete "the SSO connections belong to no organization". It does not say what their
  `SSO_PROVIDERS` entries do. The delete turns them off, unless the request keeps them on (§5.2).
- RFC: "The platform administrator types the name of the organization to confirm." The gateway checks the
  name too, and the delete also needs the confirmation of the administrator (§5.1).

## 3. Not in this work

| Left out | Note |
|---|---|
| SCIM | As in the RFC |
| A free date range | The periods of the organization console are used |
| A way to bring a deleted organization back | Delete cannot be undone (§5). Suspend is the operation that can be undone |
| To move a member to a different organization | The organization owns the account |
| Actions on many organizations at one time | Later |
| A translation of the Admin pages | The Admin Console is in English only, as today |
| Cache eviction between gateway processes | None exists. A cached lookup lives 30 to 60 seconds (§4.6) |

## 4. The status of an organization

### 4.1 Values

An organization is `active` or `suspended`. A new organization is `active`. A deleted organization has no
record, so there is no third value.

### 4.2 Who a suspension applies to

A suspension applies to **each member that is not a platform administrator**.

- An organization administrator is a member. The suspension applies.
- A platform administrator is an account with the `admin` role, or with an address in `ADMIN_EMAILS`.
  This is the rule of the organization record §4.4 for the account that keeps its password. The status
  check never refuses such an account, and a delete of its organization never suspends it. If they did,
  a platform administrator who is a member could lock the deployment: nobody could sign in to restore
  the organization. A delete can turn off the SSO connection that such an account signs in through.
  §5.2 says who that stops and how the caller is protected.
- An address in `ADMIN_EMAILS` counts whatever the role of the account says. An account with the role
  `free` gets the `admin` role with its next token. With the role only, a suspension would refuse the
  sign-in that gives that token.
- An account with a listed address and a different role (`pro`, `internal`) does not get the `admin`
  role. The suspension does not apply to it, but it cannot restore the organization. This is the rule
  of the organization record, used here without a change.

This is not the rule of `is_protected` and of `member_quota_join`, which also leave organization
administrators out. So it gets an expression of its own, in `storage/organizations.py`:

```sql
(o.status = 'suspended'
 AND (u.role IS DISTINCT FROM 'admin'
      AND NOT COALESCE(lower(u.email) = ANY($n::text[]), FALSE)))
```

`$n` is the list of the `ADMIN_EMAILS` addresses. Each statement that uses the expression passes it.

Each reader of §4.4 uses this expression and no copy of it. The delete (§5) does not read the status. It
uses the second half, the exemption, from the same definition. The comparison is `IS DISTINCT FROM`, so
that an account with no role is not left out by mistake.

The second half is the test that the organization record uses for single sign-on only
(`_SSO_ONLY_ACCOUNT`). The two rules have one definition in the code, `not_platform_administrator`. So
the accounts that a suspension applies to are the members of the organization that sign in through
single sign-on only. §4.5 depends on this.

### 4.3 Suspend and restore

`POST /admin/organizations/{id}/suspend` sets the status to `suspended`, in one transaction with the audit
row. `POST /admin/organizations/{id}/restore` sets it to `active` in the same way.

- **A suspend writes nothing else.** The accounts, the API keys and the sessions do not change. The
  gateway reads the status each time it authenticates an account (§4.4). So a restore brings everything
  back: the same keys work, and a session that was open works again.
- **A restore has a second step,** after its transaction: it clears the pause of the batches that the
  suspension paused (§4.4). This is a write in the batch store. If it fails, the status is `active` and
  these batches continue when their pause ends, 5 minutes at most. A restore of an organization that is
  active does this step again. A worker that read the status just before a restore can write its pause
  just after this step. That pause also ends after 5 minutes at most, which is why the pause is short.
- Both routes answer 200 with the organization when the status is already the one asked for. They write no
  audit row then.
- The cached store drops the cached lookups after each of the two writes (§4.6).

### 4.4 The check, at each place that authenticates an account

A suspension refuses each request of a member, not only the requests that cost money. A file upload, a
batch result and a list of models are refused too. So the check is not in the spend check, which those
routes do not run. It is where the gateway decides who the caller is.

Two readers give the answer:

- **A lookup that joins the organization today** returns one more column, `organization_suspended`, from
  the expression of §4.2. These are the key lookups and the quota context of an account
  (`get_quota_context_for_user`). They join the organization for its limit, so this costs no read. The
  column goes into each list of organization columns that sits between a lookup and its caller
  (`ORGANIZATION_COLUMNS`, `organization_fields`): a list that leaves it out would let the request pass.
- **`get_suspended_organization(user_id)`** returns the organization or nothing, for a caller that has no
  such lookup: a session, a sign-in, an internal lookup. It is one read through three primary keys: the
  membership, the organization and the account. It is
  cached for 60 seconds. It caches the answer "none" too, so a console request of an account that is in
  no organization costs no read. The entry carries the tag of the account, so each write that drops the
  cached lookups of an account drops it: a change of the role, a removal, an attach.

One function, `refuse_suspended_organization`, turns the answer into the refusal. Each of these places
calls it, at the point where it has the account and before it does anything for it:

| Place | Covers | Reader | Refusal |
|---|---|---|---|
| `_authenticate_by_api_key` | The account of the key, on `/v1/chat/completions`, `/v1/completions`, `/v1/messages`, `/v1/embeddings`, `/v1/responses`, the Qdrant proxy and the balance routes | Key lookup | The 403 below |
| `_resolve_effective_identity` | With `X-On-Behalf-Of`: the account that the request is charged to. Today this function serves the request as the service account when the target cannot be used. For a target of a suspended organization it refuses, and does not fall back | Quota context of the target. For a target with no key, the read of its organization | The 403 below |
| `verify_batch_management_identity` | Each `/v1/batches` and `/v1/files` route: create, list, read, cancel, upload, results | Key lookup | The 403 below |
| The lightweight key lookup | `/v1/models` | Key lookup | The 403 below |
| `authenticate_grant_model_call` | A model call of the cloud agent with a grant token. The grant lookup has no organization, so the check reads the quota context of the account | Quota context | The 403 below |
| `authenticate_stored_key` | Its four callers. It raises a signal of its own, not `StoredKeyRejected`, which fails a batch | Key lookup | By caller, below |
| `get_current_user` | Each console route, the playground, and each route below `/user/organization`. `/auth/logout` stays open, so a member can sign out | Account reader | A 403 in the form of the answer for an account that is not active. The detail names the organization |
| The internal lookups | `/internal/users/{id}/status`, `/agent-access`, `/model-catalog` | Account reader | The answer for a suspended account |
| The grant routes and the identity route | The mint and the renewal of an agent grant, `/v1/identity/token` | Account reader | A 403 |

```json
{
  "error": "Organization suspended",
  "code": "organization_suspended",
  "organization": "Example University",
  "contact_email": "support@example.edu",
  "message": "Organization 'Example University' is suspended. Contact an administrator of your organization."
}
```

- **The Anthropic routes** (`/v1/messages` and those below `/anthropic`) give each refusal in the
  Anthropic error form, and keep only its short text. So this refusal is a 403 with the type
  `permission_error` and the message "Organization suspended" there. It does not name the organization.
  The organization limit of #1840 has the same form on these routes. To give more there is a change of
  each refusal on these routes, and is not in this work.

- **A complete list.** The table is the list of authenticators at `38cac901`. A test keeps it complete.
  It walks each route of the application (`create_app().routes`, as the body limit test does). Each route
  must have one of the listed functions as a dependency, or be named in a table of the test. The table
  gives, for a route whose handler calls an authenticator itself, that function; and for a public or an
  Admin route, the reason. A new route that is in neither place fails the test.
- **A request that runs** when the organization is suspended finishes. The next request is refused.
- **The four callers of `authenticate_stored_key`.** The batch worker pauses the batches of the owner,
  with a reason of its own beside `monthly_quota`, for 5 minutes at most. The validator does not fail the
  batch and writes no pause, because the lease of a batch that waits for validation reads none. The
  batch keeps its lease and is tried again when the lease ends, so it is validated within 5 minutes
  after a restore. The route of the console that makes a batch answers the 403. The
  page that prepares a submission shows no quota. Both come after the session check, which refuses first.
  A restore clears the pause (§4.3). A batch whose deadline passes while it waits expires, as each paused
  batch does today.
- **A token that was given before.** An identity token lives 10 minutes and is not bound to a session. It
  stays valid until it ends. A grant token is checked at each model call with a read that is not cached,
  so the next call is refused.
- **The log.** A refused key is recorded with the credential state `organization_suspended`, beside
  `revoked`, `expired` and `user_suspended`, so an operator sees why.
- **Not an authentication failure.** The key is a live key of its account. So the refusal does not count
  for the block of an address after repeated failures: a member that calls again and again must not get
  the address of its institution blocked.

### 4.5 The check at sign-in

A member signs in through single sign-on only (the organization record §4.4). The password sign-in and
the phone sign-in refuse each member that is not a platform administrator. These are the accounts that
a suspension applies to (§4.2). So these two routes get no check of the organization: it could refuse
nobody that they admit. Three tests keep this true:

- The rule of the two routes, the rule of single sign-on only in the store and the answer of the account
  reader are compared for each kind of account, in the database.
- A member signs in with a password while the organization is active and while it is suspended, and
  gets the same refusal both times.
- The source of the two handlers must contain the refusal. If a later change removes it, the test fails
  and the check of the organization must come back.

The check of the organization is at each other route that makes or extends a session, beside the check
of the status of the account, before the route records a success or writes a row. It reads the status
without the cache.

| Route | Place | Outcome |
|---|---|---|
| SSO callback, for an account that exists | `_resolve` in `auth/sso_accounts.py` | The redirect with `suspended` |
| `/auth/refresh` and `/auth/switch` | `_usable_account` | The answer for a suspended account |
| SSO callback, first sign-in of a person | `create_sso_user` in `storage/sso.py`, for a connection of a suspended organization | No account is made. The redirect with `suspended` |
| Link of an identity, on its three paths | `link_user_identity` in `storage/sso.py`, for a connection of a suspended organization | The refusal of today for an account that is not active |

- **No new sign-in message.** Each outcome is one that the console shows today for a suspended account. So
  this work adds no string to the sign-in pages and does not change the Site UI contract.
- **The login event** of the SSO callback has the reason `sso_organization_suspended`: the callback
  writes its events as `sso_<code>`. An operator can tell it from a suspended account. The redirect to
  the console carries `suspended` for both.
- The exemption of §4.2 applies: the platform administrator signs in, and can link an identity of a
  connection of a suspended organization. A first sign-in makes no account for an `ADMIN_EMAILS`
  address in any case (the single sign-on record), so the exemption changes nothing there.

### 4.6 How fast a change applies

| Path | The process that handled the change | Each other process |
|---|---|---|
| A request with an API key, a grant model call, the batch worker, the account that a request on behalf of an account is charged to | The next request | The next request. These reads are not cached |
| `/v1/models`, and the service key of a request on behalf of an account | The next request | 30 seconds at most |
| A session, an internal lookup, a grant route | The next request | 60 seconds at most |
| A new sign-in | The next attempt | The next attempt |

- The handling process drops its cached key lookups and its cached answers of
  `get_suspended_organization` (`invalidate_auth_caches`).
- The same bounds apply to a restore, to a delete, and to a change of the `admin` role of a member.
- Suspend, restore and delete go into the list of organization writes that must evict
  (`tests/unit/storage/test_cache_per_account_invalidation.py`).

### 4.7 With the other states

| Case | Result |
|---|---|
| The account is suspended and the organization is suspended | The refusal of the account. A restore of the organization does not change the account |
| A member that the organization removed, then the organization is suspended and restored | The member stays removed |
| A restore of a removed member while the organization is suspended | Allowed. The account is active, and the suspension still applies |
| An attach of a connection while the organization is suspended | Allowed. The new members are refused as the others are. No session has to be ended, because the check is at each request |
| An account loses the `admin` role while its organization is suspended | The suspension applies from the next read: 60 seconds at most. Not if its address is in `ADMIN_EMAILS` |
| An address is put in `ADMIN_EMAILS` while the organization of its account is suspended | The suspension stops for the account at the next read with the new list that is not cached: 60 seconds at most after the process has the list |
| The organization turned a model off for a member, or revoked a key of a member (the organization record §7.5, §7.6) | A suspend and a restore change neither |
| A sign-in at the same moment as a suspend | If the session is made, its first request is refused |
| A change of the limit, the name or the administrators while suspended | Allowed |

## 5. Delete

`DELETE /admin/organizations/{id}` cannot be undone. The body has `confirm_name`, the confirmation of the
administrator, and `disable_connections`. `disable_connections` is true when the body does not give it.

### 5.1 What the route checks

In this order:

1. The organization exists. If not, a 404.
2. `confirm_name` is equal to its name, as an exact string. If not, a 409. The console asks the platform
   administrator to type the name, and sends the name that it compared the text with. The gateway checks
   too: a wrong id in a call must not delete an organization.
3. With `disable_connections`: the `SSO_PROVIDERS` document can be changed, and the caller keeps a way to
   sign in (§5.2). If not, a 409.
4. The confirmation of the administrator (`confirm_administrator`), as for the administrator role. It has
   three forms: a recent code of the authenticator, the password, or a new sign-in at the identity
   provider for an account that has only that. `ADMIN_TOKEN` cannot confirm, so it cannot delete an
   organization.

- **The confirmation comes last.** A wrong name must not use it: a password attempt counts for the
  sign-in limits, and a new sign-in at the identity provider confirms one change only.
- **A refusal after check 4** (§5.2, §5.3) has used the confirmation. The administrator confirms again.
- **Each 409 has a `code`,** because five causes share the status: `confirm_name_mismatch`,
  `settings_invalid`, `last_sign_in`, `settings_changed` and `organization_changed`. A failure after the
  route started to switch the entries off answers 500 with the code `delete_incomplete`. Its message
  says if the entries are off, or if they can be off (§5.2, §5.3).
- **Two more codes, for the settings.** `settings_unavailable` (503): the gateway has no settings store,
  or cannot read the version of the settings. `settings_refused`: the settings refused the write for a
  reason that is not a change in between; the status is the one that the settings gave. In both cases
  nothing is deleted.

Then the route does its writes. The write that can be undone goes first.

### 5.2 First: the SSO connections are turned off

With `disable_connections`, the route sets `enabled` to `false` in each `SSO_PROVIDERS` entry that is a
connection of the organization.

**Why.** After the delete the connections belong to no organization. If an entry with `auto_create` stays
on, a person who signs in through it for the first time gets a personal account. Nobody decided to give
that person an account. If the entry is off, the sign-in does not start, and no account is made.

**How the route writes it:**

- Before check 3, the route reads the settings again from the database (`app_config.refresh`), and
  takes their version. The view in memory is not enough: a process can be 10 seconds behind. The route
  takes the text of the document later, after check 4. The write is conditional on the version, so a
  document that changed in between is not written over (`settings_changed`).
- If the document does not validate, the route answers 409 (`settings_invalid`) and writes nothing. The
  gateway reads such a document as "no providers". Without this check each connection would look like
  an entry that is gone, the delete would turn nothing off, and the entries would be on again when
  someone corrects the document.
- The route changes the parsed document and writes it with `app_config.update` and the version that it
  read, as the removal of a model from the catalog does. If a different save changed the settings in
  between, the settings answer 409 (`settings_changed`), and no save is lost.
- The route then writes the `config.update` audit row. `app_config.update` does not write this row: the
  handler of `PATCH /admin/config` does. The two handlers use one function for it. The row has no
  `organization_id`: its details hold the whole document, and an organization administrator reads the
  rows of the organization.
- A save of this setting needs the confirmation of the administrator. Check 4 gives it.
- **If a step here fails,** the route stops and deletes nothing. If the setting was stored before the
  failure, the answer says that the entries are off. If the settings fail in a way that is not a
  refusal, they can have stored the change: the answer then says that the entries may be off. In that
  case no `config.update` row is written, because the route did not get the change back. The setting
  itself holds who changed it. A save in Admin Settings loses its row in the same way.
- **No write is needed** for an entry that is off, or for a connection whose id the document does not
  have any more. The delete does not look for an entry that got a new id: that entry is not a connection
  of the organization. If no entry changes, the route writes no setting and no row.
- **A document from the environment.** If `SSO_PROVIDERS` has no stored row, the write stores one, and
  the stored row then applies in place of the environment. A save in Admin Settings does the same.
- **With `disable_connections: false`** the route skips check 3 and these writes. The entries stay as
  they are.

**Who a turned-off entry stops.** Nobody starts a sign-in through it: not a member, not an account that
is in no organization, not a platform administrator.

- An account that signed in only through it needs a password. **Forgot password** sets one, and needs
  outgoing email. The account cannot set a password in its settings: that confirmation goes to the
  identity provider, and the entry is off.
- A session that is open continues, and `/auth/refresh` extends it.
- A member that is a platform administrator is not suspended (§4.2). A join does not take the password
  of a platform administrator. It is stopped in this way only if it has no password, no verified phone
  and no other identity. The platform administrator who deletes can turn the entry on again.

**The caller is not locked out.** Check 3 refuses the delete (`last_sign_in`) when the delete would turn
off an entry that serves an identity of the caller, and the caller has no password, no verified phone,
and no identity that an entry which stays on serves.

- "Serves" is the rule of `provider_for_identity`, not a comparison of ids: an identity follows an entry
  whose id changed.
- The check uses the snapshot that the write depends on.
- This rule is new. The disconnect of an identity counts each other identity, also one whose entry is
  off. Here that rule would let the caller pass and then lose the sign-in.
- The account of the caller is `get_admin_user_id`. `ADMIN_TOKEN` has no account: check 3 passes it, and
  check 4 refuses it. With `ADMIN_TWO_FACTOR_REQUIRED`, the token is refused before check 1.

**How fast, and what stays open:**

| Case | Result |
|---|---|
| The process that handled the delete | The entries are off at once |
| Each other process | At its next read of the settings: 10 seconds, or 20 if one read fails. Until then a first sign-in through an entry with `auto_create` can make a personal account. No test shows this bound, because one process cannot see it |
| A callback that the gateway already handles | It passed the check of `enabled` and waits for the identity provider. It can finish after the delete, and it makes a personal account if the entry has `auto_create` |
| A link that waits to be accepted | It can be accepted for 10 minutes at most. The acceptance does not read `enabled` |
| A later save in Admin Settings | It can turn an entry on again. This is the decision of the administrator who saves |

### 5.3 Then: the delete, in one transaction

1. It takes three locks, in this order: the membership lock (`ATTACH_LOCK_SQL`), the key lock for all
   accounts (`_lock_all_account_keys`), and the row of the organization.
   - **The membership lock.** An attach takes it, and a sign-in shares it. So no sign-in and no attach
     makes a member of the organization while it is deleted. The row lock alone is not enough: a
     sign-in that waits for it would insert a membership after the delete.
   - **The key lock for all accounts** is the form for a write that covers many accounts. A lock for each
     account can use up the lock table. The lock alone is not enough: a creation of a key that waits
     for it would insert a live key after the delete. So `create_key` reads the account after it has
     the lock, and refuses when the account row exists and is `suspended`. This check does not refuse
     an id with no account row. The two routes that make a key answer 409 then.
   - **The order.** From a read of the code at `38cac901`, no path takes these two locks in the other
     order, so the order cannot deadlock.
2. Under the locks it reads the organization again. Nothing is deleted in these cases:
   - The organization is gone: a 404. This is the second of two deletes at the same time.
   - The name is not `confirm_name` any more: a 409 (`organization_changed`).
   - With `disable_connections`, the organization has a connection that §5.2 did not handle: a 409
     (`organization_changed`). An attach came between the writes. A second delete turns the entry of
     that connection off.
3. It suspends each member account whose status is `active` and that is not a platform administrator
   (§4.2): the status becomes `suspended`, the sessions end, the API keys are revoked, and the
   suspension message names the organization. These are set statements, not one statement for each member. It locks the
   membership rows before it reads the statuses. A restore of a member by its organization holds such a
   row: without the lock the delete would not wait for it, and would leave that account active.
4. It removes the organization. The memberships and the connections go with it (`ON DELETE CASCADE`). A
   membership holds the limit and the model list of the organization for the member, so these go too. It
   removes the counter rows of the organization in `spend_counters`, because no foreign key does.
5. It writes one audit row, `organization.delete`. The details hold the name, the number of members, the
   number of accounts that step 3 suspended, the ids of the connections, and the ids of the entries that
   §5.2 turned off.

- **If this transaction fails,** the organization stays and its connections are off. The answer says so.
  A second delete writes a setting only for an entry that is on, and deletes. The route does not turn
  the entries on again by itself: that write can fail too, and then the answer could not say what state
  the entries are in.
- **If the process stops between the writes,** there is no answer, and the state is the same: the
  organization stays and the entries are off. The `config.update` row shows it. If the process stopped
  before that row, the setting holds who changed it.
- **The cached lookups.** The store returns the ids of the accounts of step 3. The cached store drops
  their session answers, as a removal of a member does. For each account, it drops the cached account
  row, each cached key lookup and each cached answer of `get_suspended_organization`
  (`invalidate_auth_caches`, §4.6). A member that step 3 did not suspend has cached lookups with the
  organization in them too, and a cached account row with the model list of the organization for it.
  The store does not name these members, so the row of each account goes, as after an attach.
- **The answer** has the number of the accounts of step 3 and the ids of the entries that §5.2 turned
  off.

### 5.4 After a delete

**What stays:** the accounts, their usage rows, their projects, and the audit rows of the organization.
The platform administrator reads these audit rows with the `organization_id` filter of
`GET /admin/audit-log`.

- The platform administrator can make one of these accounts active again in **Admin → Users**, as for
  each suspended account. The account is then in no organization. Its API keys stay revoked. The
  suspension message of the account names the organization, so the accounts of a deleted organization
  can be found. The audit row holds their number, not their ids.
- **Such an account has no password.** The join removed it (the organization record §4.4), and its SSO
  entry is off unless the request kept it on. The account is in no organization now, so the rule of
  single sign-on only does not apply to it any more. Its owner sets a password with **Forgot password**,
  which needs outgoing email, or signs in through an SSO entry that is on.
- An account that waits for approval is not changed. It has no key and no session. It stays in
  **Admin → Users** with the status "Pending approval", in no organization, and the platform administrator
  approves or rejects it as today. To suspend it would close this path: approve and reject apply only to
  an account that waits, and a suspended account that is made active skips the approval.
- The SSO connections belong to no organization. Their `SSO_PROVIDERS` entries are off, unless the
  request kept them on. The list of §6 shows them in `available_connections`, each with its state.
- To use such a connection again, the platform administrator attaches it to an organization and then
  turns the entry on in Admin Settings. In the other order, an entry with `auto_create` makes personal
  accounts until it is attached.
- When such a connection is attached to a new organization, the accounts that match the member rule
  become its members, the suspended accounts too. They stay suspended. The join removes a password that
  such an account set in between.
- A cost that is settled at the same moment can write a counter row of the organization again. Nothing
  reads this row, because the id is not used again.

## 6. The list of organizations

`GET /admin/organizations?period=&status=&search=&sort_by=&order=&limit=&offset=`

- `period`: `1d`, `7d` or `30d`. `status`: `all`, `active` or `suspended`. `search`: a part of the name, or
  of a domain of a connection. `sort_by`: `name`, `members`, `requests`, `spend` or `month_spend`.
- Each row: id, name, domains, status, number of members, requests and spend in the period, share of
  spend, the spend of this month and the limit. The fields that a row has today stay.
- `totals` for the period, over all organizations: organizations, members, requests, spend.
  `status_counts` over all organizations. Neither follows `search` or `status`, so the figures above the
  table do not change when the administrator filters the table. `total` is the number of rows after the
  filters.
- The share of a row is its spend divided by the spend of all organizations in the period.
- `available_connections`: the `SSO_PROVIDERS` entries that belong to no organization, with the id, the
  name, the domains and if the entry is on. The **New organization** dialog and the attach control need
  this list, and no route gives it today.

**Where the figures come from:**

| Figure | Source | Same as the Overview of the organization? |
|---|---|---|
| Spend in the period | `user_daily_cost` of the members | Yes. The same statement, grouped by organization |
| Requests in the period | `user_usage_daily` and its live part (the rollup behind **Admin → Users**). A row is each logged request, also free and failed | The same count |
| Members | `org_memberships` | Yes |
| Spend of this month | The counter row of the organization in `spend_counters` | Yes |

- **The figures of a member are of its whole account.** #1840 changed this on 2026-11-23: the days and the
  requests from before the join count too, as in **Admin → Users**. The list follows. Only the counter
  of the monthly limit starts at the join, so the spend of this month can be less than the spend of the
  members in the same days.

- The rollup has a reader for all time and a reader for one account. The list needs a new reader for a
  range of days, by account. The key of the rollup is the account and the day, so this is one statement.
- The list does not read `api_logs`, except the live part of the rollup: today, and a day that the rollup
  did not fill yet.
- The route runs one grouped statement for each source. The number of statements does not grow with the
  number of organizations.
- The domains come from `SSO_PROVIDERS`, not from a table. So the route applies `search`, the sort and the
  page to the rows in memory. The cost follows the number of organizations, not the usage.

## 7. One organization, for the platform administrator

The functions behind the organization console take the organization as an argument. This work adds thin
routes that pass the organization of the path:

| Route below `/admin/organizations/{id}` | Same as, in the organization record §11.1 |
|---|---|
| `GET /overview`, `GET /members`, `GET /members/summary`, `GET /members/{user_id}` | The routes of the same name below `/user/organization` |
| `GET /usage/volume`, `GET /usage/requests`, `GET /usage/models`, `GET /usage/members`, `GET /audit-log` | The same |
| `PUT /members/{user_id}/monthly-limit`, `PUT /default-member-limit` | The same |
| `POST /members/{user_id}/remove`, `POST /members/{user_id}/restore` | The same |
| `GET /members/{user_id}/models`, `PUT /members/{user_id}/models` | The same (the organization record §7.5) |
| `POST /members/{user_id}/keys/{key_id}/revoke` | The same (the organization record §7.6) |

This is each route below `/user/organization` at `38cac901`, except the route that reads the organization
itself, which `GET /admin/organizations/{id}` gives.

- **The rules do not change.** A member action does not apply to an organization administrator or to an
  account with the `admin` role: the answer is a 409. To remove an organization administrator, the platform
  administrator removes the role first. A restore applies only to a member that was removed.
- **A removal here is the action of the organization.** It sets the mark of a removal, so an organization
  administrator can restore the member. To stop an account in a way that the organization cannot undo, the
  platform administrator suspends it in **Admin → Users**, as today.
- **The limit of a member** that these routes write is the limit of the organization for that member. The
  monthly quota of the account stays in **Admin → Users**.
- **The model list of a member** that these routes write is the list of the organization for that member.
  The list of the platform administrator stays in **Admin → Users**. A model is off when it is in one of
  the two lists.
- **An unknown organization** gets a 404. No route returns the content of a request.
- **A suspended organization** can be read and changed through these routes. They are for the platform
  administrator, and §4 does not apply to them.
- **Audit.** Each write records the platform administrator as the actor and has the `organization_id`. In
  the audit log, an organization administrator sees the actor as "Platform administrator", as today. The
  platform administrator sees the account of the actor. This is the one difference between a body of
  these routes and the body of the organization route.

## 8. Admin → Users

#1840 gives the gateway part and #1841 the page, since 2026-11-23:

- Each row of `GET /admin/users` has `organization`: `{id, name, role}` for a member, `null` for each
  other account. `GET /admin/users/{user_id}/detail` has the same field.
- The parameter `organization_id`: an id, or `none` for the accounts that are in no organization.
- The page has an **Organization** column and a filter.

This work adds one field: `status` in `organization`, the status of the organization. An account of a
suspended organization has the account status `active`, and the gateway refuses it. Without this field
the page does not show why. The field is new, so existing clients continue to work.

## 9. Who may call what

- Each route of this record uses `verify_admin_access`, as each Admin route does. So `ADMIN_TOKEN` can
  call them where it can call an Admin write today, except the delete.
- Suspend and restore need no further confirmation. They write one field and can be undone.
- Delete needs the confirmation of §5. The confirmation also covers its change of `SSO_PROVIDERS`, a
  setting that Admin Settings changes only with a confirmation.
- An organization administrator gets a 403 from each route below `/admin`, as today.

New audit actions. The actor is the platform administrator and the target is the organization:
`organization.suspend`, `organization.restore`, `organization.delete`.

## 10. HTTP surface

| Route | Change |
|---|---|
| `GET /admin/organizations` | The parameters and the fields of §6 |
| `POST /admin/organizations/{id}/suspend`, `POST /admin/organizations/{id}/restore` | New (§4.3) |
| `DELETE /admin/organizations/{id}` | New (§5). The body has `disable_connections`, true by default |
| The routes of §7 | New |
| `GET /admin/organizations/{id}` | The field `status` |
| `GET /admin/users`, `GET /admin/users/{user_id}/detail` | `status` in the field `organization` (§8) |
| Each route that takes an API key or a grant token | The 403 of §4.4 |
| Each route that takes a session | The 403 of §4.4 for a session |
| The SSO callback, `/auth/refresh` and `/auth/switch` | The refusals of §4.5, each with an outcome that exists today |
| The internal lookups, the grant routes and `/v1/identity/token` | The answers of §4.4 |

## 11. Data model

One expand revision. The previous release does not read the column.

```sql
ALTER TABLE orgs
    ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'suspended'));
```

- No other column. The time and the actor of a suspension are in the audit row, and no page shows them
  from the organization.
- The name is `0024_organization_status` after `0023_orgs`. The number is the next free one
  when the pull request merges. `dev` is at `0022` today and one open pull request more adds a `0023`, so
  the numbers can move. The check for one head decides.

## 12. Console

- **A new tab**, `/dashboard/admin/organizations`: the totals, the status tabs with their counts, the
  search, the table with a sort on each figure, and the **New organization** dialog (a name, one SSO
  connection or more from `available_connections`, and a monthly limit in the ledger currency).
- **One organization**, `/dashboard/admin/organizations/[id]/…`: the five pages. This is the first route
  of the Admin Console that has an id in the path.
- **The same views, with the organization as an input.** Today the views of #1841 read the organization of
  the caller: the API module has one root, and the query keys have no id. This work gives them a scope: the
  root of the API, the id in each query key, the links, and who looks (an organization administrator or the
  platform administrator). The id in the key matters: without it, the organization of the caller and an
  organization that the platform administrator opened would share cached data.
- **No second copy.** #1841 moved the parts that the two consoles both show to
  `components/features/console-shared/`. The pages of this work use these parts and the views of the
  organization console. The member panel has the model switch and the key revoke; for the platform
  administrator they call the routes of §7.
- **Words for the reader.** Some sentences of #1841 speak to an organization administrator ("ask the
  platform administrator"). For the platform administrator these places show a sentence of their own.
  The English text of an existing string does not change, so the Chinese catalog stays valid.
- **Settings, for the platform administrator only:** the connections (attach, detach), the administrators
  (give with the confirmation of today, remove), the limit of the organization, and the status. Suspend and
  restore ask one time. Delete asks for the name (`requireText`) and for the confirmation of the
  administrator. The dialog sends as `confirm_name` the name that it compared the typed text with:
  `requireText` does not give the typed text to the action today. It has a box **Turn off the SSO
  connections of this organization**, which is checked. The text below the box says what each choice
  does (§5.2). A refusal of the gateway shows its message in the dialog, by its `code` (§5.1).
- **A suspended organization** shows a line on each of its pages that says what the gateway refuses.
- **A paused batch.** The gateway pauses a batch of a member with the reason `organization_suspended`.
  The batch pages get a sentence for this reason, beside the sentences for a spent quota. A member does
  not see it while the organization is suspended, because the session is refused.
- **Each dialog** follows the limit dialog of #1841: while the data loads the action is off; after a
  failed read it says so and offers a retry; a retry shows that it runs.
- **Admin → Users:** the column and the filter are in #1841. This work adds a mark beside the name of an
  organization that is suspended, in the column and in the details of the account.
- The Admin Console stays in English. No string of the sign-in pages changes.

## 13. Acceptance criteria

Each line is a behavior that can be observed. "Hand" says if the walk of §14 repeats it.

| # | Behavior | Shown by | Hand |
|---|---|---|---|
| 1 | After a suspend, the next request with the API key of a member gets the 403 of §4.4. One case for each place of the table of §4.4 that takes a key: an inference route, a batch route that costs nothing (list, cancel), a file upload, `/v1/models` and a balance route. A request on behalf of a member gets the same 403 and is not served as the service account | Database test of the authenticators that these routes depend on. The test of criterion 4 ties each route to one of them. The walk of §14 sends two of the routes over HTTP | Yes |
| 2 | A grant model call gets the same 403 at the next call. The mint and the renewal of a grant and `/v1/identity/token` refuse. The internal lookups answer as for a suspended account | Database test for the grant call, the three internal lookups and the mint. The renewal and the identity route: the database test of the function that they share, and a check that each handler calls it | No |
| 3 | A session of a member gets the 403 of §4.4 on a console route, on the playground and on `/user/organization`. `/auth/logout` works for this session | Database test for a console route, `/user/organization` and the sign-out. The playground by the test of criterion 4: it depends on the same function | Yes |
| 4 | Each route of the application has a listed authenticator as a dependency or is named in the table of the test of §4.4. The test fails for a route that is in neither place | The test, and one case with a route added on purpose | No |
| 5 | The status check does not refuse a member that is a platform administrator, by the `admin` role or by an `ADMIN_EMAILS` address: not for a key, not for a session, not at sign-in. A delete does not suspend it. An organization administrator is refused | Database tests. For each of the two kinds of platform administrator: the authenticators of a key, a session over HTTP and an SSO sign-in; and a refresh, which gives an account with a listed address the role. Of the store: each lookup, and the delete | No |
| 6 | A suspend writes the status and the audit row and no other row. After a restore, the same key gets a 200 and a session that was open works, with no other step | Database test | Yes |
| 7 | A running batch of a member pauses at a suspend, and a batch that waits for validation is not failed. After a restore both continue within 5 minutes: the running batch when the restore lifts its pause, or when a pause that was written just after the restore ends; the waiting batch when its lease ends | Unit test of the worker, which also holds the pause to 5 minutes. Database tests of the validator, of the lift of a pause and of the restore route | No |
| 8 | The SSO callback refuses a member with the outcome of a suspended account. The login event has the reason `sso_organization_suspended`. No success is recorded and no row of the account is written. A password sign-in of a member gets the same refusal while the organization is active and while it is suspended, and makes no session. The accounts that a suspension applies to are the accounts that sign in through single sign-on only | Database test for the SSO callback and for the password. Database test that compares the rule of the two routes, the rule in the store and the account reader. Unit tests: one definition for the two rules, and the refusal in the source of the two handlers. The phone sign-in is not changed by this work, and its test is in #1840 | SSO |
| 9 | `/auth/refresh` and `/auth/switch` refuse a member whose session is live | Database test | No |
| 10 | A first sign-in through a connection of a suspended organization makes no account and no membership. A link on each of its three paths is refused | Database test | Yes |
| 11 | On one process, a key lookup and an answer of the account reader that were cached before a suspend are not used after it. The same for a restore, for a delete, and for a change of the `admin` role of a member. The three writes are in the list of writes that evict. The account reader caches the answer "none". After a delete, the cached account row of each account is read again | Unit test of the cached store. Database test for the change of the role | No |
| 12 | Delete answers, in this order: 404 for an unknown id; 409 for a wrong `confirm_name`; 409 for an `SSO_PROVIDERS` document that does not validate; 409 for a caller who would lose the only way to sign in. Each 409 has its `code`, and none of the four uses the confirmation. A caller with a password, or with an identity that an entry which stays on serves, passes the last check. With the right name, a caller whose confirmation is missing or wrong is refused, and `ADMIN_TOKEN` is refused. In each case no row and no setting changes | Database test with the settings store | The wrong name |
| 13 | After a delete with no `disable_connections` in the body: no organization, membership, connection owner or counter row of it remains. Each member account that was `active` and not a platform administrator is suspended, with its sessions ended, its keys revoked and a message that names the organization. An account that waited for approval is not changed. The usage rows, the projects and the audit rows remain, and the `organization_id` filter reads these audit rows. Each `SSO_PROVIDERS` entry of its connections is off: `POST /auth/sso/discover` answers `available: false` for an address of the organization, and a sign-in through the entry does not start. One `config.update` row with no `organization_id` records the change of the setting. The `organization.delete` row and the answer hold the entries and the number of suspended accounts | Database test with the settings store attached and `SSO_PROVIDERS` as a stored row | Yes |
| 14 | A creation of a key for a member that waits for the delete is refused. After the delete no live key of a suspended account remains. The two routes that make a key answer 409 for a suspended account | Database test with two connections. Database test of the Admin route. For the route of the account, a test in which the store refuses | No |
| 15 | A failure inside the transaction of a delete, forced at the audit insert, leaves each row as it was. The entries are off, and the answer says so. A second delete completes and writes no setting | Database test with the settings store and a forced failure | No |
| 16 | With `disable_connections: false`: the check of the caller is skipped, no setting changes and no `config.update` row is written. A first sign-in through an entry with `auto_create` then makes a personal account, as today. The same delete with an entry that was off before writes no setting and no `config.update` row | Database test with the settings store | No |
| 17 | A save of the settings between the read and the write of the route: 409 `settings_changed`, and nothing is deleted. An attach or a change of the name between the checks and the transaction: 409 `organization_changed`, and nothing is deleted. Two deletes at the same time: one deletes, and the other gets a 404. A sign-in that waits for the delete makes no member | Database test with the settings store and two connections | No |
| 18 | After a delete, an account that is made active again in **Admin → Users** is in no organization, and its keys stay revoked. A former member can have a password again, which a member cannot. When the connection is attached to a new organization, the accounts that match the member rule become members and stay suspended | Database test | No |
| 19 | For one organization and one period, the list gives the spend, the requests, the members and the spend of this month that the Overview gives | Database test that reads both | Yes |
| 20 | Totals, share, status counts, search by name and by domain, each sort in both orders, the pages, and `available_connections` | Database test | Yes |
| 21 | The list runs the same number of statements for one organization and for twenty | Database test that counts statements | No |
| 22 | Each read route of §7, except the audit log, returns the body that the organization route returns to an administrator of that organization. Each route of §7 answers 401, 403, 403 and 200 to no session, a member, an organization administrator and the platform administrator, and 404 for an unknown id. They answer for a suspended organization too | Database test | No |
| 23 | A member action of the platform administrator writes an audit row with the `organization_id`. The organization administrator sees "Platform administrator" as the actor, and can restore a member that the platform administrator removed | Database test | Yes |
| 24 | `GET /admin/users` and the detail of an account have the status of the organization in `organization`: `suspended` after a suspend, `active` after a restore, and no organization after a delete | Database test | Yes |
| 25 | **Admin → Users** marks an account of a suspended organization, in the column and in the details | Console test | No |
| 26 | The tab shows the totals, the rows, the status tabs, the search and the sort. A new organization appears in the list | Console tests with a stubbed API | Yes |
| 27 | The five pages show the organization of the path. Data of a second organization that was opened before is not shown | Console test with two ids | Yes |
| 28 | Settings: attach, detach, give and remove the administrator role, the limit, suspend, restore, and delete with the typed name and the box for the connections, which is checked at the start. Each dialog while its data loads, after a failed read, and in a retry | Console tests | Yes |
| 29 | The catalog test and `npm run i18n:check` pass, and no existing English string changed | The two checks | No |
| 30 | The previous release runs on a database that has the new revision | The migration test, and the restore drill of CI | No |

## 14. The walk by hand

A local deployment with Keycloak as the identity provider, two members and one organization
administrator, as for #1829.

1. Make an organization on the **Organizations** tab. Sign in as the two members. Send a request with a
   key of each.
2. Read the list for `7d`. Open the organization. Compare the Overview with the row.
3. Suspend. Send a chat request with a key, then `GET /v1/models` with it: the 403 two times. In the
   session of a member that was open, load a page: refused. Try to sign in through SSO: the refusal.
4. Restore. The same key works. The session that was open works.
5. As the platform administrator, set the limit of a member and remove a member. As the organization
   administrator, read the audit log and restore the member.
6. Open **Admin → Users**. Filter by the organization, then by no organization.
7. Delete with a wrong name, then with the right name and the code. Read **Admin → Users**: the members
   are suspended. Read `SSO_PROVIDERS` in Admin Settings: the entry is off. Send `POST /auth/sso/discover`
   with an address of the organization: the answer has `available: false`. The sign-in page has no
   **Log in with SSO** if this was the only provider.

## 15. Release and rollback

- **No organization, no change.** The tab shows an empty list. No account is a member, so no check finds
  a suspended organization.
- **The revision only adds** one column with a default. The previous release does not read it.
- **A rollback of the gateway removes the checks.** A suspended organization stays `suspended` in its
  record, and the previous release does not refuse its members. After the upgrade is done again, the
  suspension applies again.
- **A delete is not rolled back** by a rollback of the gateway. It changed accounts and keys. It also
  turned `SSO_PROVIDERS` entries off. These are a setting, and Admin Settings turns them on again.
- **The console needs the gateway.** A newer gateway with an older console works: the routes are not
  called. A newer console with an older gateway shows errors on the new tab, so the gateway goes first.

## 16. Delivery

Three pull requests. Each one can be reviewed alone.

1. **Gateway, the status:** the revision, suspend, restore, delete with its write of the settings
   (§5.2), the checks of §4.4 and §4.5, the
   eviction, the audit actions, their tests, the operator guide and this record. This is the part that
   decides who the gateway refuses, so it has no page and no list with it.
2. **Gateway, the read side:** the list of §6, the routes of §7, the status of the organization in
   `GET /admin/users` and their tests. It changes no check.
3. **Console:** the tab, the pages of one organization, Settings, the mark in **Admin → Users**, and
   their tests.

Order: 1 needs #1840. 2 needs 1. 3 needs #1841 and 2. Between the merges nothing that a user sees
changes, except that the routes of 1 and 2 answer.

## 17. Alternatives considered

| Question | Not chosen | Why |
|---|---|---|
| Where the status is checked | In the spend check (`SpendCheck`), where the limit of the organization is checked | The routes that cost nothing do not run it: file upload, batch results, list and cancel, the balance routes, `/v1/models`. A suspended member could use them |
| Where the status is checked | In the `WHERE` of each key lookup, beside the status of the account | The key would look invalid. The member gets a 401 "invalid API key", makes a new key, and gets the same answer. A refusal that names the organization tells the member who to contact |
| How to suspend an organization | Remove each member, as the organization console does | A removal revokes the API keys, and a restore does not bring them back. A suspend must be undone with one step |
| What a suspend does to open sessions | End them one time, at the suspend | A session that starts after it is missed: a member that an attach adds, an account that loses the `admin` role, a sign-in at the same moment. The check at each request has no such case, and a restore brings the sessions back |
| An `admin` account that is a member | Refuse it as each member | The platform administrator could then lock the deployment with one click |
| Who is a platform administrator for a suspension | The `admin` role only | The organization record counts an `ADMIN_EMAILS` address too, and such an account with the role `free` gets the `admin` role with its next token. With the role only, a suspension refuses the sign-in that gives that token, and a delete suspends the account |
| The password and the phone sign-in | A check of the organization there too | These routes refuse each account that a suspension applies to (§4.5). Such a check could refuse nobody that the route admits, so no test through the route can show that it works |
| The states | A third value `deleted`, with the record kept | Nothing would read it, and the RFC says that a delete cannot be undone. The audit rows keep the history |
| The confirmation of a delete | The typed name in the console only | A call with a wrong id would then delete an organization. The gateway must check too |
| The connections after a delete | Keep the `SSO_PROVIDERS` entries on, and tell the operator | With an entry that has `auto_create`, a person who signs in for the first time would get a personal account that nobody decided to give. The delete turns the entries off, and the request can keep them on |
| What a delete turns off | Only the `auto_create` of each entry | An account could still sign in through the identity provider of an organization that is gone, and a sign-in could still link accounts by `auto_link`. `enabled` is the switch that the SSO guide gives to stop a provider |
| Who the check of the caller covers | Each account with the `admin` role that would lose its only way to sign in | The deployment is not locked while the caller can sign in, and the caller can turn an entry on again. A check for each such account would stop the delete for a reason that the caller cannot correct alone |
| The order of the two writes of a delete | The delete first, then the entries | If the second write failed, the entries would stay on after a delete that cannot be undone, and a second delete could not correct it |
| The two writes of a delete | One transaction | `app_config.update` opens a transaction of its own, with a lock and a version check, and reloads the settings after it. To put the delete inside it would need a second way to write a setting |
| After a failed delete | Turn the entries on again | That write can fail too. A second delete is the way forward |
| The requests in the list | Count `api_logs`, as the Overview does | One read of the list would scan the log of each member of each organization. **Admin → Users** left this path for the rollup (#1567, #1572) |
| Search and sort of the list | In SQL | The domains are in `SSO_PROVIDERS`, not in a table. The number of organizations is small |
| The message at sign-in | A new message "your organization is suspended" | A new sign-in string changes the Site UI contract. The message of a suspended account says what the person must do: contact an administrator |
| The date range | A control with two dates | The console has none, and the periods of the organization console make the list and the Overview comparable |

## 18. Decisions on the open questions

The author of the RFC answered these on 2026-11-21. No question is open.

| # | Question | Decision |
|---|---|---|
| 1 | The base, while #1840 and #1841 are open | This work builds on their branches. It does not wait for the two merges |
| 2 | Three pull requests or two | Three, as in §16 |
| 3 | The refusal of a key | The 403 that names the organization (§4.4), not the 401 of an invalid key |
| 4 | The confirmation of a delete | The name, and the confirmation of the administrator. `ADMIN_TOKEN` is refused (§5.1) |
| 5 | The connections after a delete | The delete turns the `SSO_PROVIDERS` entries off, so that a first sign-in makes no personal account. The request can keep them on (§5.2). The first proposal of this record kept them on |
| 6 | The accounts that wait for approval | A delete does not change them (§5) |

On 2026-11-23, #1840 and #1841 got a second part, which a maintainer asked for. It changed four things
that this record depends on. The author of the RFC decided these on the same day:

| # | What changed in #1840 and #1841 | Decision |
|---|---|---|
| 7 | The tables are `orgs`, `org_sso_connections` and `org_memberships`, and the revision is `0023_orgs` | This work is moved to `38cac901`. Its revision follows `0023_orgs` (§11) |
| 8 | A member signs in through single sign-on only | The password and the phone sign-in get no check of the organization (§4.5). The first text of this record had one at each |
| 9 | A platform administrator is the `admin` role or an `ADMIN_EMAILS` address | The exemption of §4.2 uses the same rule. The first text of this record used the role only |
| 10 | **Admin → Users** has the organization of an account, and the organization console has more pages and member actions | §8 adds only the status. §7 covers each route of the organization console. The figures of §6 are of the whole account, as #1840 made them |
