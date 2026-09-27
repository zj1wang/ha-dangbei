"""TV entity so the projector can be driven by the iOS remote (HomeKit).

Only a media_player with ``device_class: tv`` is turned into a HomeKit
"Television" accessory (``homekit/accessories.py::get_accessory`` ->
``TelevisionMediaPlayer``), and the iOS remote reaches Home Assistant through
three completely different paths -- see the table in README:

    key                    HomeKit side                       this entity
    --------------------------------------------------------------------------
    arrows / select /      RemoteKey -> fires                  presses the
    back                   homekit_tv_remote_key_pressed       matching Dangbei
                                                              button entity
    play / pause           RemoteKey -> media_player.media_*  presses the
                           service (never fires an event)     "home" button
    power                  Active -> media_player.turn_on/off the Dangbei
                                                              remote entity

The last two cannot be done from a blueprint: ``set_remote_key()`` short
circuits ``play_pause`` into a media_player service call, and the power key
does not use the RemoteKey characteristic at all.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DangbeiRuntimeData
from .const import (
    CMD_BACK,
    CMD_DOWN,
    CMD_HOME,
    CMD_LEFT,
    CMD_OK,
    CMD_RIGHT,
    CMD_UP,
    DOMAIN,
)
from .device_info import projector_device_info

_LOGGER = logging.getLogger(__name__)

# Home Assistant fires this for every remote key it does not handle itself.
EVENT_HOMEKIT_TV_REMOTE_KEY_PRESSED = "homekit_tv_remote_key_pressed"
ATTR_KEY_NAME = "key_name"
ATTR_ENTITY_ID = "entity_id"

# iOS remote key name -> Dangbei command (the button entity suffix).
REMOTE_KEY_TO_COMMAND: dict[str, str] = {
    "arrow_up": CMD_UP,
    "arrow_down": CMD_DOWN,
    "arrow_left": CMD_LEFT,
    "arrow_right": CMD_RIGHT,
    "select": CMD_OK,
    "back": CMD_BACK,
}

# PLAY | PAUSE is what makes HomeKit route the remote's play/pause key into the
# media_player services (instead of ignoring it). No volume features on purpose:
# declaring VOLUME_STEP would add volume buttons to the iOS remote that this
# integration does not translate into projector volume keys.
SUPPORT_DANGBEI_TV = (
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the TV entity used by the iOS remote."""
    runtime: DangbeiRuntimeData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DangbeiTV(entry, runtime)])


class DangbeiTV(CoordinatorEntity[bool], MediaPlayerEntity):
    """The projector as a TV, so HomeKit exposes an iOS remote for it."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "tv"
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, runtime: DangbeiRuntimeData) -> None:
        super().__init__(runtime.projector_coordinator)
        self._entry_id = entry.entry_id
        self._coordinator = runtime.projector_coordinator
        self._client = runtime.client
        self._attr_unique_id = f"{entry.entry_id}_tv"
        self._attr_device_info = projector_device_info(entry)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        return SUPPORT_DANGBEI_TV

    @property
    def state(self) -> str:
        """Power state of the projector, taken from the power coordinator."""
        return STATE_ON if self._coordinator.effective_state else STATE_OFF

    # ── Remote: arrows / select / back ───────────────────────────────────────
    # HomeKit fires an event per key press instead of calling a service, so we
    # listen for it and press the matching Dangbei button entity. Pressing the
    # button entity (instead of sending the command ourselves) keeps the
    # projector protocol in one place and lets users see/reuse the entities.
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_HOMEKIT_TV_REMOTE_KEY_PRESSED, self._handle_remote_key
            )
        )

    @callback
    def _handle_remote_key(self, event: Event) -> None:
        if event.data.get(ATTR_ENTITY_ID) != self.entity_id:
            return
        command = REMOTE_KEY_TO_COMMAND.get(event.data.get(ATTR_KEY_NAME))
        if command is None:
            # Keys we do not map (exit / rewind / information / ...) are ignored.
            return
        self.hass.async_create_task(self._async_press_button(command))

    # ── Power key ───────────────────────────────────────────────────────────
    # The iOS remote's power key writes the HomeKit ``Active`` characteristic,
    # which Home Assistant translates into media_player.turn_on / turn_off
    # (homekit/type_media_players.py::TelevisionMediaPlayer.set_on_off). Both
    # actions are forwarded to the Dangbei ``remote`` entity, which owns the
    # real power logic (power_off + confirm dialog, or the ESP32 BLE wake-up).
    #
    # Both directions are idempotent on purpose. ``turn_on`` must NEVER toggle:
    # iOS also writes ``Active = 1`` by itself (it wants to wake the accessory,
    # no matter which key was pressed), so a toggle would switch off a
    # projector that is already running.
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the projector on through the remote entity's wake-up."""
        if self._coordinator.effective_state:
            return
        await self._async_call_remote("turn_on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the projector off through the remote entity."""
        if not self._coordinator.effective_state:
            # Already off: skip, otherwise power_off would sit through the
            # WebSocket reconnect retries (the projector only listens on 6689
            # while it is running).
            return
        await self._async_call_remote("turn_off")

    # ── Remote: play / pause -> Dangbei "home" ──────────────────────────────
    # HomeKit never fires an event for this key: as soon as the entity declares
    # PLAY | PAUSE, TelevisionMediaPlayer.set_remote_key() picks one of these
    # three services from the current state and returns. All of them are mapped
    # to the projector's home key.
    async def async_media_play(self) -> None:
        await self._async_press_button(CMD_HOME)

    async def async_media_pause(self) -> None:
        await self._async_press_button(CMD_HOME)

    async def async_media_play_pause(self) -> None:
        await self._async_press_button(CMD_HOME)

    # ── Helpers ────────────────────────────────────────────────────────────
    async def _async_press_button(self, command: str) -> None:
        """Press the Dangbei button entity of this entry for ``command``."""
        entity_id = self._async_registry_entity_id(
            "button", f"{self._entry_id}_{command}"
        )
        if entity_id is None:
            # Button entity not registered (yet): fall back to the raw command
            # so the key still does something.
            await self._client.async_send_command(command)
            return
        # Not blocking: a key press should return immediately, even while the
        # projector is still booting (the WebSocket reconnect would take ~15s).
        await self.hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: entity_id}, blocking=False
        )

    async def _async_call_remote(self, service: str) -> None:
        """Call ``remote.turn_on`` / ``remote.turn_off`` on our remote entity."""
        entity_id = self._async_registry_entity_id(
            "remote", f"{self._entry_id}_remote"
        )
        if entity_id is None:
            raise HomeAssistantError(
                "Dangbei remote entity is not available; cannot change power state."
            )
        await self.hass.services.async_call(
            "remote", service, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    def _async_registry_entity_id(self, domain: str, unique_id: str) -> str | None:
        """Look up one of our entities by unique_id (survives renames)."""
        return er.async_get(self.hass).async_get_entity_id(domain, DOMAIN, unique_id)
