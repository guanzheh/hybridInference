"""Switch ``SSO_PROVIDERS`` entries off from code, as a save in Admin Settings does.

A delete of an organization switches off the entries that were its SSO
connections: an entry that stays on after its organization is gone would give
a personal account to the next person who signs in through it for the first
time. The write goes the way a save in Admin Settings goes
(:func:`serving.config.app_config.update`): the whole document is validated,
it is stored at a version, and every process applies it. So this module only
reads the document freshly, changes ``enabled``, and hands it over.

The document is not in the operational store, and a write to it is not part of
a transaction there. The caller orders its writes around that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.config import app_config, documents, sso as sso_config

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from serving.config.revisions import SourceVersion

_KEY = "SSO_PROVIDERS"


class SsoSwitchError(Exception):
    """The entries could not be switched off.

    ``status_code`` and ``code`` say why, for the route to answer with:
    ``settings_unavailable`` (503) without a settings store, ``settings_invalid``
    (409) for a document that does not validate, ``settings_changed`` (409)
    when another save came between the read and the write. Nothing was stored.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        """Record the answer the caller gives."""
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


async def read_version() -> SourceVersion:
    """Read the settings again from the database; return the version they are now at.

    The view a process holds can be one refresh interval old, and a write made
    on it would be refused, or would switch off the wrong entries. After this,
    :func:`serving.config.sso.get_sso_document` is the stored document, and the
    version returned is the one a write of it must be conditional on.

    Raises:
        SsoSwitchError: 503 without a settings store. 409 when the stored
            document does not validate: the gateway reads such a document as
            "no providers", so nothing in it could be found and switched off,
            and the entries would be on again when someone corrects it.
    """
    if app_config.attached_store() is None:
        raise SsoSwitchError(
            503,
            "settings_unavailable",
            "Configuration storage is unavailable: the database is not connected.",
        )
    await app_config.refresh()
    # No await between these reads: they describe one snapshot.
    version = app_config.source_version()
    problem = app_config.validation_problem(_KEY)
    if problem is not None:
        raise SsoSwitchError(
            409,
            "settings_invalid",
            f"{_KEY} is not valid, so its entries cannot be switched off. Correct it in Admin "
            "Settings first, or keep the SSO connections on.",
        )
    if version is None:
        raise SsoSwitchError(
            503, "settings_unavailable", "The version of the configuration cannot be read."
        )
    return version


def entries_on(provider_ids: Iterable[str]) -> list[str]:
    """The ids among *provider_ids* that have an entry that is switched on.

    An id whose entry is off already, or that the document does not have any
    more, needs no write. Read from the document in memory: call
    :func:`read_version` first.
    """
    on = {provider.id for provider in sso_config.enabled_providers()}
    return [provider_id for provider_id in provider_ids if provider_id in on]


def _switched_off(
    document: dict[str, Any], provider_ids: Sequence[str]
) -> tuple[dict[str, Any], list[str]]:
    """Return *document* with ``enabled`` false for *provider_ids*, and the ids it changed."""
    providers = document.get("providers")
    if not isinstance(providers, list):
        return document, []
    wanted, changed, entries = set(provider_ids), [], []
    for entry in providers:
        # An entry without ``enabled`` is on: that is the default of the schema.
        if isinstance(entry, dict) and entry.get("id") in wanted and entry.get("enabled", True):
            entry = {**entry, "enabled": False}
            changed.append(entry["id"])
        entries.append(entry)
    return {**document, "providers": entries}, changed


async def switch_off(
    provider_ids: Sequence[str], *, version: SourceVersion, updated_by: str
) -> tuple[list[str], list[app_config.ConfigChange]]:
    """Store the document with these entries switched off, if the settings are still at *version*.

    Returns the ids of the entries it switched off and the stored changes, for
    the caller to write to the audit log as a save in Admin Settings does. Both
    are empty, and nothing is stored, when no entry needed a change.

    Raises:
        SsoSwitchError: 409 ``settings_changed`` when a save came since
            *version* was read (nothing is stored, and that save is not lost);
            409 ``settings_invalid`` when the document cannot be saved; the
            status the settings give for anything else they refuse.
    """
    if not provider_ids:
        return [], []
    try:
        document = documents.parse(app_config.configured_value(_KEY) or "")
        updated, changed = _switched_off(document, provider_ids)
        if changed:
            documents.check("sso", updated)
    except documents.DocumentError as exc:
        raise SsoSwitchError(409, "settings_invalid", f"{_KEY} cannot be updated: {exc}") from None
    if not changed:
        return [], []
    try:
        changes = await app_config.update(
            {_KEY: documents.encode(updated)}, updated_by=updated_by, expected_version=version
        )
    except app_config.ConfigUpdateError as exc:
        conflict = exc.status_code == 409 and exc.current_version is not None
        raise SsoSwitchError(
            exc.status_code, "settings_changed" if conflict else "settings_refused", exc.detail
        ) from None
    return changed, changes
