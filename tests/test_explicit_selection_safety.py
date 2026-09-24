"""A selector whose value is a mutation target starts on "Select a device…".

Home Assistant's frontend gives a required select field its first option
before anybody touches it (`computeInitialHaFormData`, frontends 20260729.5
and 20260826.7 pinned by Home Assistant 2026.8.0 and 2026.9.3). A bare
Submit therefore sends that first option as if it had been chosen, and the
flow cannot tell the difference. So the first option of every selector that
decides what a replacement record points at, or which related reference is
removed, is a placeholder the handlers refuse, and the Asset picker opened
from a hub starts on the Asset it came from.

These tests own that contract: the placeholder is what the frontend would
submit, refusing it touches nothing, and an explicit choice still performs
exactly the mutation it did before.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.data_entry_flow import _BaseFlowManagerView

from custom_components.device_lifecycle.config_flow import (
    NOT_SELECTED,
    DeviceLifecycleOptionsFlow,
    _explicit_selection,
)
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_EFFECTIVE_DATE,
    CONF_NOTES,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
)
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import capture_reloads
from .test_ha_relationship_options_flow import _external_device
from .test_options_flow import _manager, _options_flow

HUB_STEP = "manage_asset_menu"
# The replacement submenu opens from the Lifecycle & replacement section.
SECTION_STEP = "asset_lifecycle_replacement_menu"
REPLACEMENT_STEP = "asset_replacement"
HA_STEP = "ha_relationship"
PLACEHOLDER = {"value": NOT_SELECTED, "label": "Select a device…"}
TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "device_lifecycle"
    / "translations"
)

# Step id -> whether the Asset being managed is the successor. A
# relationship is created only from the new Asset, so there is one form.
DIRECTIONS = {
    "replacement_replaces": True,
}
NOTHING_CHOSEN = (
    pytest.param({CONF_REPLACEMENT_TARGET_ASSET_UUID: NOT_SELECTED}, id="placeholder"),
    pytest.param({}, id="omitted"),
    pytest.param({CONF_REPLACEMENT_TARGET_ASSET_UUID: ""}, id="empty"),
    pytest.param({CONF_REPLACEMENT_TARGET_ASSET_UUID: None}, id="none"),
)
NO_DEVICE_CHOSEN = (
    pytest.param({CONF_DEVICE_ID: NOT_SELECTED}, id="placeholder"),
    pytest.param({}, id="omitted"),
    pytest.param({CONF_DEVICE_ID: ""}, id="empty"),
)


def _sent_fields(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a form schema exactly as Home Assistant sends it to the frontend.

    The serializer itself changed between 2026.8 and 2026.9, so this goes
    through Home Assistant's own flow view rather than naming either one.
    """
    return _BaseFlowManagerView._prepare_result_json(None, result)["data_schema"]


def _frontend_initial_data(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Model the frontend's untouched form data for the fields used here.

    A suggested value wins, then a default. An optional field stays empty.
    A required select starts on its first option. Other required fields
    start empty, which is true of the text, device, entity and area selectors
    these forms use.
    """
    data: dict[str, Any] = {}
    for field in fields:
        suggested = (field.get("description") or {}).get("suggested_value")
        if suggested is not None:
            data[field["name"]] = suggested
        elif "default" in field:
            data[field["name"]] = field["default"]
        elif not field.get("required"):
            continue
        elif "select" in field.get("selector", {}):
            options = field["selector"]["select"]["options"]
            if options:
                first = options[0]
                data[field["name"]] = first if isinstance(first, str) else first["value"]
        else:
            data[field["name"]] = ""
    return data


def _untouched_submission(result: dict[str, Any]) -> dict[str, Any]:
    """Return what Submit sends when nobody touched the form.

    The frontend leaves out empty values rather than sending them.
    """
    return {
        key: value
        for key, value in _frontend_initial_data(_sent_fields(result)).items()
        if value not in ("", None)
    }


def _marker(result: dict[str, Any], field: str) -> vol.Marker:
    """Return one form field's schema marker."""
    for marker in result["data_schema"].schema:
        if marker == field:
            return marker
    raise AssertionError(f"Missing field {field}")


def _options(result: dict[str, Any], field: str) -> list[dict[str, str]]:
    """Return one select field's options in order."""
    validator = result["data_schema"].schema[_marker(result, field)]
    assert isinstance(validator, selector.SelectSelector)
    return list(validator.config["options"])


def _suggested(result: dict[str, Any], field: str) -> Any:
    """Return the value a re-rendered form keeps for one field."""
    description = _marker(result, field).description or {}
    return description.get("suggested_value")


async def _on_asset(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str,
) -> DeviceLifecycleOptionsFlow:
    """Return a flow sitting on one Asset's hub."""
    flow, _entry = _options_flow(hass, manager)
    hub = await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    assert hub["step_id"] == HUB_STEP
    return flow


async def _flow_manager_on_asset(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str,
) -> str:
    """Open the real OptionsFlow through the FlowManager on one Asset's hub."""
    _unused, entry = _options_flow(hass, manager)
    opened = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = opened["flow_id"]
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "manage_asset"}
    )
    hub = await hass.config_entries.options.async_configure(
        flow_id, {CONF_ASSET_UUID: asset_uuid}
    )
    assert hub["step_id"] == HUB_STEP
    return flow_id


async def _three_assets(manager: AssetStoreManager):
    """Return the managed Asset and two other candidates."""
    current = await manager.async_create_manual_asset(name="Current unit")
    first = await manager.async_create_manual_asset(name="Alpha unit")
    second = await manager.async_create_manual_asset(name="Beta unit")
    return current, first, second


async def _related_asset(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    device_registry: dr.DeviceRegistry,
    *,
    stale_first: bool = False,
):
    """Return an Asset with one live and one stale related reference."""
    asset = await manager.async_create_manual_asset(name="Related unit")
    _owner, live = _external_device(
        hass,
        device_registry,
        key=f"explicit-live-{stale_first}",
        name="Bench meter",
    )
    references = ["gone-related", live.id] if stale_first else [live.id, "gone-related"]
    for device_id in references:
        await manager.async_add_related_device(asset["asset_uuid"], device_id)
    return asset, live


def _refuse_mutation(manager: AssetStoreManager, method: str) -> AsyncMock:
    """Make one manager mutation fail the test if anything calls it."""
    guard = AsyncMock(side_effect=AssertionError(f"{method} must not be called"))
    setattr(manager, method, guard)
    return guard


def test_the_frontend_model_preselects_the_first_option_of_a_required_select() -> None:
    """The rule this module guards against, applied to a plain select field.

    Without a default, the frontend's untouched value is the first option,
    which is why a real Asset or device can never be the first option of a
    mutation target.
    """
    result = {
        "type": FlowResultType.FORM,
        "data_schema": vol.Schema(
            {
                vol.Required("choice"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value="first", label="First"),
                            selector.SelectOptionDict(value="second", label="Second"),
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("optional_choice"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=["one", "two"])
                ),
            }
        ),
    }

    assert _frontend_initial_data(_sent_fields(result)) == {"choice": "first"}
    assert _untouched_submission(result) == {"choice": "first"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        (NOT_SELECTED, None),
        ("11111111-1111-4111-8111-111111111111", "11111111-1111-4111-8111-111111111111"),
        ("a-device-id", "a-device-id"),
    ],
)
def test_only_a_real_value_counts_as_a_selection(value: Any, expected: Any) -> None:
    """Nothing chosen and the placeholder are the same thing to a handler."""
    assert _explicit_selection(value) == expected


@pytest.mark.parametrize("step_id", DIRECTIONS)
async def test_replacement_target_starts_on_the_placeholder(
    hass: HomeAssistant,
    step_id: str,
) -> None:
    """The untouched target is the placeholder, never a candidate Asset."""
    manager = _manager(hass)
    current, first, second = await _three_assets(manager)
    flow = await _on_asset(hass, manager, current["asset_uuid"])

    form = await getattr(flow, f"async_step_{step_id}")()
    options = _options(form, CONF_REPLACEMENT_TARGET_ASSET_UUID)

    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == step_id
    assert options[0] == PLACEHOLDER
    assert _marker(form, CONF_REPLACEMENT_TARGET_ASSET_UUID).default() == NOT_SELECTED
    # Candidates follow in name order, and the managed Asset is not one.
    assert [option["value"] for option in options[1:]] == [
        first["asset_uuid"],
        second["asset_uuid"],
    ]
    assert current["asset_uuid"] not in {option["value"] for option in options}
    untouched = _untouched_submission(form)
    assert untouched[CONF_REPLACEMENT_TARGET_ASSET_UUID] == NOT_SELECTED
    assert untouched[CONF_REPLACEMENT_REASON] == "unknown"


async def test_the_placeholder_reads_in_finnish(hass: HomeAssistant) -> None:
    """The placeholder speaks the reader's language on both selectors."""
    hass.config.language = "fi"
    manager = _manager(hass)
    current, _first, _second = await _three_assets(manager)
    await manager.async_add_related_device(current["asset_uuid"], "gone-related")
    flow = await _on_asset(hass, manager, current["asset_uuid"])

    replacement = await flow.async_step_replacement_replaces()
    removal = await flow.async_step_remove_related_device()

    finnish = {"value": NOT_SELECTED, "label": "Valitse laite…"}
    assert _options(replacement, CONF_REPLACEMENT_TARGET_ASSET_UUID)[0] == finnish
    assert _options(removal, CONF_DEVICE_ID)[0] == finnish


@pytest.mark.parametrize("step_id", DIRECTIONS)
@pytest.mark.parametrize("nothing_chosen", NOTHING_CHOSEN)
async def test_replacement_without_a_chosen_target_changes_nothing(
    hass: HomeAssistant,
    step_id: str,
    nothing_chosen: dict[str, Any],
) -> None:
    """Same form, a field error, and no manager call, write, reload or result."""
    manager = _manager(hass)
    current, _first, _second = await _three_assets(manager)
    flow = await _on_asset(hass, manager, current["asset_uuid"])
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()
    create = _refuse_mutation(manager, "async_create_asset_replacement")

    with capture_reloads(hass) as reload:
        result = await getattr(flow, f"async_step_{step_id}")(
            {
                **nothing_chosen,
                CONF_REPLACEMENT_REASON: "failure",
                CONF_EFFECTIVE_DATE: "2026-01-02",
                CONF_NOTES: "Kept while choosing",
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step_id
    assert result["errors"] == {
        CONF_REPLACEMENT_TARGET_ASSET_UUID: "replacement_target_required"
    }
    create.assert_not_called()
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before
    assert flow._last_result is None
    assert flow._selected_asset_uuid == current["asset_uuid"]
    # The rest of the form survives the correction.
    assert _suggested(result, CONF_REPLACEMENT_REASON) == "failure"
    assert _suggested(result, CONF_EFFECTIVE_DATE) == "2026-01-02"
    assert _suggested(result, CONF_NOTES) == "Kept while choosing"
    # And the target is back on the placeholder, not on a real Asset.
    assert _suggested(result, CONF_REPLACEMENT_TARGET_ASSET_UUID) == NOT_SELECTED
    assert _untouched_submission(result)[CONF_REPLACEMENT_TARGET_ASSET_UUID] == (
        NOT_SELECTED
    )


@pytest.mark.parametrize(("step_id", "current_is_successor"), DIRECTIONS.items())
async def test_an_explicit_target_records_exactly_one_relationship(
    hass: HomeAssistant,
    step_id: str,
    current_is_successor: bool,
) -> None:
    """A chosen candidate still records one relationship, once, in its direction."""
    manager = _manager(hass)
    current, _first, second = await _three_assets(manager)
    flow = await _on_asset(hass, manager, current["asset_uuid"])
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        result = await getattr(flow, f"async_step_{step_id}")(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: second["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
            }
        )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == REPLACEMENT_STEP
    assert result["description_placeholders"]["result"] == "Replacement updated."
    manager._store.async_save.assert_awaited_once()
    reload.assert_called_once()
    assert flow._selected_asset_uuid == current["asset_uuid"]
    records = manager.replacement_records_for_asset(current["asset_uuid"])
    assert len(records) == 1
    expected = (
        (second["asset_uuid"], current["asset_uuid"])
        if current_is_successor
        else (current["asset_uuid"], second["asset_uuid"])
    )
    assert (
        records[0]["predecessor_asset_uuid"],
        records[0]["successor_asset_uuid"],
    ) == expected
    assert NOT_SELECTED not in json.dumps(manager._data)


@pytest.mark.parametrize("step_id", DIRECTIONS)
async def test_an_untouched_submit_through_home_assistant_records_nothing(
    hass: HomeAssistant,
    step_id: str,
) -> None:
    """End to end: what the frontend sends untouched, or an API client omits."""
    manager = _manager(hass)
    current, first, _second = await _three_assets(manager)
    flow_id = await _flow_manager_on_asset(hass, manager, current["asset_uuid"])
    for hop in (SECTION_STEP, REPLACEMENT_STEP):
        await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": hop}
        )
    form = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": step_id}
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()
    create = _refuse_mutation(manager, "async_create_asset_replacement")

    with capture_reloads(hass) as reload:
        for submission in (
            _untouched_submission(form),
            {CONF_REPLACEMENT_REASON: "failure"},
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: NOT_SELECTED,
                CONF_REPLACEMENT_REASON: "failure",
            },
        ):
            refused = await hass.config_entries.options.async_configure(
                flow_id, submission
            )
            assert refused["type"] is FlowResultType.FORM
            assert refused["step_id"] == step_id
            assert refused["errors"] == {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: "replacement_target_required"
            }

    create.assert_not_called()
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before

    del manager.async_create_asset_replacement
    with capture_reloads(hass) as reload:
        chosen = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: first["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
            },
        )

    assert chosen["step_id"] == REPLACEMENT_STEP
    manager._store.async_save.assert_awaited_once()
    reload.assert_called_once()
    assert len(manager.replacement_records_for_asset(current["asset_uuid"])) == 1


async def test_related_removal_starts_on_the_placeholder(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """No stored reference, live or stale, is what an untouched form removes."""
    manager = _manager(hass)
    asset, live = await _related_asset(
        hass, manager, device_registry, stale_first=True
    )
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    form = await flow.async_step_remove_related_device()
    options = _options(form, CONF_DEVICE_ID)

    assert form["step_id"] == "remove_related_device"
    assert options[0] == PLACEHOLDER
    assert _marker(form, CONF_DEVICE_ID).default() == NOT_SELECTED
    assert [option["value"] for option in options[1:]] == ["gone-related", live.id]
    assert _untouched_submission(form) == {CONF_DEVICE_ID: NOT_SELECTED}


@pytest.mark.parametrize("nothing_chosen", NO_DEVICE_CHOSEN)
async def test_related_removal_without_a_choice_removes_nothing(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    nothing_chosen: dict[str, Any],
) -> None:
    """Same form, a field error, and every reference still stored."""
    manager = _manager(hass)
    asset, live = await _related_asset(
        hass, manager, device_registry, stale_first=True
    )
    flow = await _on_asset(hass, manager, asset["asset_uuid"])
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()
    remove = _refuse_mutation(manager, "async_remove_related_device")

    with capture_reloads(hass) as reload:
        result = await flow.async_step_remove_related_device(nothing_chosen)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "remove_related_device"
    assert result["errors"] == {CONF_DEVICE_ID: "related_device_required"}
    remove.assert_not_called()
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": "gone-related", "role": "related"},
        {"device_id": live.id, "role": "related"},
    ]
    assert flow._last_result is None
    assert flow._selected_asset_uuid == asset["asset_uuid"]
    assert _untouched_submission(result) == {CONF_DEVICE_ID: NOT_SELECTED}


async def test_an_explicitly_chosen_live_reference_is_removed(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Removal itself is unchanged once somebody picks the reference."""
    manager = _manager(hass)
    asset, live = await _related_asset(hass, manager, device_registry)
    flow = await _on_asset(hass, manager, asset["asset_uuid"])
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        result = await flow.async_step_remove_related_device({CONF_DEVICE_ID: live.id})

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == HA_STEP
    assert result["description_placeholders"]["result"] == (
        "Related Home Assistant device removed."
    )
    manager._store.async_save.assert_awaited_once()
    reload.assert_called_once()
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": "gone-related", "role": "related"}
    ]


async def test_a_stale_reference_is_removed_only_when_chosen(
    hass: HomeAssistant,
) -> None:
    """A reference to a device Home Assistant no longer has cannot be re-added.

    So even as the only related reference it is never what an untouched
    form removes, and choosing it still removes exactly that one.
    """
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Stale only")
    await manager.async_add_related_device(asset["asset_uuid"], "gone-related")
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    form = await flow.async_step_remove_related_device()
    untouched = await flow.async_step_remove_related_device(
        _untouched_submission(form)
    )

    assert _options(form, CONF_DEVICE_ID) == [
        PLACEHOLDER,
        {"value": "gone-related", "label": "Home Assistant device unavailable"},
    ]
    assert untouched["errors"] == {CONF_DEVICE_ID: "related_device_required"}
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": "gone-related", "role": "related"}
    ]

    with capture_reloads(hass) as reload:
        removed = await flow.async_step_remove_related_device(
            {CONF_DEVICE_ID: "gone-related"}
        )

    assert removed["step_id"] == HA_STEP
    reload.assert_called_once()
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == []


async def test_an_untouched_removal_through_home_assistant_removes_nothing(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """End to end: neither the frontend's untouched data nor an empty call."""
    manager = _manager(hass)
    asset, live = await _related_asset(hass, manager, device_registry)
    flow_id = await _flow_manager_on_asset(hass, manager, asset["asset_uuid"])
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": HA_STEP}
    )
    form = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "remove_related_device"}
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()
    remove = _refuse_mutation(manager, "async_remove_related_device")

    with capture_reloads(hass) as reload:
        for submission in (_untouched_submission(form), {}):
            refused = await hass.config_entries.options.async_configure(
                flow_id, submission
            )
            assert refused["step_id"] == "remove_related_device"
            assert refused["errors"] == {CONF_DEVICE_ID: "related_device_required"}

    remove.assert_not_called()
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before
    assert live.id in {
        reference["device_id"]
        for reference in manager.asset(asset["asset_uuid"])["ha_device_refs"]
    }


async def test_choosing_another_device_starts_on_the_current_one(
    hass: HomeAssistant,
) -> None:
    """From a hub, the picker offers the Asset it came from, by UUID."""
    manager = _manager(hass)
    current, first, second = await _three_assets(manager)
    flow = await _on_asset(hass, manager, current["asset_uuid"])

    picker = await flow.async_step_manage_asset()
    options = _options(picker, CONF_ASSET_UUID)

    assert picker["step_id"] == "manage_asset"
    assert _suggested(picker, CONF_ASSET_UUID) == current["asset_uuid"]
    assert _untouched_submission(picker) == {CONF_ASSET_UUID: current["asset_uuid"]}
    # Values stay the immutable UUID and labels stay `Name · DLxxxx`.
    assert options == [
        {"value": asset["asset_uuid"], "label": f"{asset['name']} · {asset['asset_id']}"}
        for asset in (first, second, current)
    ]


async def test_opening_the_current_device_again_changes_nothing(
    hass: HomeAssistant,
) -> None:
    """Open without a new choice: the same hub, no write, reload or result."""
    manager = _manager(hass)
    current, _first, _second = await _three_assets(manager)
    flow = await _on_asset(hass, manager, current["asset_uuid"])
    picker = await flow.async_step_manage_asset()
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        hub = await flow.async_step_manage_asset(_untouched_submission(picker))

    assert hub["type"] is FlowResultType.MENU
    assert hub["step_id"] == HUB_STEP
    assert hub["description_placeholders"]["asset"] == (
        f"Current unit · {current['asset_id']}"
    )
    assert hub["description_placeholders"]["result"] == ""
    assert flow._selected_asset_uuid == current["asset_uuid"]
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before


async def test_an_explicit_other_device_opens_its_hub(hass: HomeAssistant) -> None:
    """Changing the choice still moves to the chosen Asset."""
    manager = _manager(hass)
    current, first, _second = await _three_assets(manager)
    flow = await _on_asset(hass, manager, current["asset_uuid"])
    await flow.async_step_manage_asset()

    hub = await flow.async_step_manage_asset({CONF_ASSET_UUID: first["asset_uuid"]})

    assert hub["step_id"] == HUB_STEP
    assert flow._selected_asset_uuid == first["asset_uuid"]
    assert hub["description_placeholders"]["asset"] == (
        f"Alpha unit · {first['asset_id']}"
    )


async def test_the_first_picker_keeps_the_frontend_default(hass: HomeAssistant) -> None:
    """Before any Asset is selected, the picker only navigates.

    No current Asset exists to start on, so nothing is suggested and the
    frontend's own first option stands.
    """
    manager = _manager(hass)
    _current, first, _second = await _three_assets(manager)
    flow, _entry = _options_flow(hass, manager)

    picker = await flow.async_step_manage_asset()

    assert _suggested(picker, CONF_ASSET_UUID) is None
    assert _untouched_submission(picker) == {CONF_ASSET_UUID: first["asset_uuid"]}


async def test_a_vanished_current_device_is_not_suggested(hass: HomeAssistant) -> None:
    """A deleted Asset falls back to the picker without pointing at itself."""
    manager = _manager(hass)
    current, _first, _second = await _three_assets(manager)
    flow = await _on_asset(hass, manager, current["asset_uuid"])
    manager._data["assets"].pop(current["asset_uuid"])
    manager._data["lifecycle_events"].pop(
        current["lifecycle"]["current_event_uuid"], None
    )

    picker = await flow.async_step_manage_asset_menu()

    assert picker["step_id"] == "manage_asset"
    assert picker["errors"] == {"base": "asset_missing"}
    assert _suggested(picker, CONF_ASSET_UUID) is None


async def test_opening_the_current_device_through_home_assistant(
    hass: HomeAssistant,
) -> None:
    """End to end: hub, Choose another device, untouched Open, same hub."""
    manager = _manager(hass)
    current, _first, _second = await _three_assets(manager)
    flow_id = await _flow_manager_on_asset(hass, manager, current["asset_uuid"])
    picker = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "manage_asset"}
    )
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        hub = await hass.config_entries.options.async_configure(
            flow_id, _untouched_submission(picker)
        )

    assert hub["step_id"] == HUB_STEP
    assert hub["description_placeholders"]["asset"] == (
        f"Current unit · {current['asset_id']}"
    )
    assert hub["description_placeholders"]["result"] == ""
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


@pytest.mark.parametrize(
    ("language", "submit", "target_required", "device_required"),
    [
        (
            "en",
            "Open",
            "Select a device. No replacement is recorded until you choose one.",
            (
                "Select the related Home Assistant device to remove. "
                "Nothing is removed until you choose one."
            ),
        ),
        (
            "fi",
            "Avaa",
            "Valitse laite. Korvaussuhdetta ei kirjata ennen valintaa.",
            (
                "Valitse poistettava liittyvä Home Assistant -laite. "
                "Mitään ei poisteta ennen valintaa."
            ),
        ),
    ],
)
def test_the_new_copy_exists_in_both_languages(
    language: str,
    submit: str,
    target_required: str,
    device_required: str,
) -> None:
    """The picker's button and both refusals read in the reader's language."""
    options = json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )["options"]

    assert options["step"]["manage_asset"]["submit"] == submit
    assert options["error"]["replacement_target_required"] == target_required
    assert options["error"]["related_device_required"] == device_required
