"""Switching ``SSO_PROVIDERS`` entries off: what changes in the document, and what does not.

The whole path -- the fresh read, the version, the stored row, the audit row --
is shown with the real settings store in
``tests/integration/test_organization_delete_postgres.py``. Here: the document
itself, and the refusals that need no database.
"""

from __future__ import annotations

import pytest

from serving.config import app_config, sso_switch


def _entry(provider_id: str, **fields) -> dict:
    return {"id": provider_id, "type": "oidc", "domains": [f"{provider_id}.example.edu"], **fields}


def test_only_the_entries_named_and_switched_on_change() -> None:
    document = {
        "providers": [
            _entry("campus"),  # no ``enabled``: on, by the default of the schema
            _entry("staff", enabled=True),
            _entry("off-already", enabled=False),
            _entry("another", enabled=True),
        ],
        "note": "kept",
    }

    updated, changed = sso_switch._switched_off(
        document, ["campus", "staff", "off-already", "not-in-the-document"]
    )

    assert changed == ["campus", "staff"]
    assert [(entry["id"], entry.get("enabled")) for entry in updated["providers"]] == [
        ("campus", False),
        ("staff", False),
        ("off-already", False),
        ("another", True),
    ]
    # Every other field and key is as it was, and the input is not changed.
    assert updated["note"] == "kept"
    assert updated["providers"][0]["domains"] == ["campus.example.edu"]
    assert "enabled" not in document["providers"][0]
    assert document["providers"][1]["enabled"] is True


@pytest.mark.parametrize("document", [{}, {"providers": None}, {"providers": "campus"}])
def test_a_document_without_a_list_of_providers_has_nothing_to_change(document) -> None:
    assert sso_switch._switched_off(document, ["campus"]) == (document, [])


@pytest.mark.asyncio
async def test_nothing_to_switch_off_stores_nothing(monkeypatch) -> None:
    async def update(*_args, **_kwargs):
        raise AssertionError("nothing may be stored")

    monkeypatch.setattr(app_config, "update", update)
    monkeypatch.setattr(
        app_config, "configured_value", lambda _key: '{"providers": [{"id": "campus"}]}'
    )

    assert await sso_switch.switch_off([], version=None, updated_by="admin") == ([], [])
    # The entry asked for is not in the document any more.
    assert await sso_switch.switch_off(["gone"], version=None, updated_by="admin") == ([], [])


@pytest.mark.asyncio
async def test_without_a_settings_store_nothing_is_read(monkeypatch) -> None:
    monkeypatch.setattr(app_config, "attached_store", lambda: None)

    with pytest.raises(sso_switch.SsoSwitchError) as refused:
        await sso_switch.read_version()

    assert (refused.value.status_code, refused.value.code) == (503, "settings_unavailable")


@pytest.mark.asyncio
async def test_a_refusal_of_the_settings_keeps_its_status_and_a_conflict_gets_its_code(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        app_config, "configured_value", lambda _key: '{"providers": [{"id": "campus"}]}'
    )
    monkeypatch.setattr(sso_switch.documents, "check", lambda _kind, _value: None)
    refusals = iter(
        [
            app_config.ConfigUpdateError(409, "changed since", current_version=object()),
            app_config.ConfigUpdateError(503, "no database"),
        ]
    )

    async def update(*_args, **_kwargs):
        raise next(refusals)

    monkeypatch.setattr(app_config, "update", update)

    with pytest.raises(sso_switch.SsoSwitchError) as conflict:
        await sso_switch.switch_off(["campus"], version=object(), updated_by="admin")
    with pytest.raises(sso_switch.SsoSwitchError) as unavailable:
        await sso_switch.switch_off(["campus"], version=object(), updated_by="admin")

    assert (conflict.value.status_code, conflict.value.code) == (409, "settings_changed")
    assert (unavailable.value.status_code, unavailable.value.code) == (503, "settings_refused")
