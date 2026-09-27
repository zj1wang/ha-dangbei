"""TV (media player) entity so the projector can be controlled via the iOS remote.

Reference: xiaomi_tv integration's HomeKit TV handling.

How the iOS remote (HomeKit TV) maps to this entity:
  - arrow / select / back / information -> `homekit_tv_remote_key_pressed` event
    -> forwarded to the matching Dangbei button entity (up/down/left/right/ok/back/menu)
  - play / pause / play_pause -> `media_player.media_play|media_pause|media_play_pause`
    -> Dangbei **home** (主页) button
  - power -> `media_player.turn_on|turn_off`
    -> the Dangbei remote entity (remote.dangbei_xxx) power switch
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DangbeiRuntimeData
from .client import DangbeiClient, DangbeiWolClient
from .const import (
    CMD_BACK,
    CMD_DOWN,
    CMD_HOME,
    CMD_LEFT,
    CMD_MENU,
    CMD_OK,
    CMD_POWER_OFF,
    CMD_RIGHT,
    CMD_UP,
    CMD_VOLUME_DOWN,
    CMD_VOLUME_UP,
    CONF_BLUETOOTH_MAC,
    DOMAIN,
)
from .device_info import projector_device_info

_LOGGER = logging.getLogger(__name__)

# iOS 遥控器方向键（homekit_tv_remote_key_pressed 的 key_name）-> 当贝遥控命令
REMOTE_KEY_TO_CMD = {
    "arrow_up": CMD_UP,
    "arrow_down": CMD_DOWN,
    "arrow_left": CMD_LEFT,
    "arrow_right": CMD_RIGHT,
    "select": CMD_OK,
    "back": CMD_BACK,
    "information": CMD_MENU,
}

SUPPORT_DANGBEI_TV = (
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.PLAY_PAUSE
    | MediaPlayerEntityFeature.VOLUME_STEP
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the TV (media player) entity for the projector."""
    runtime: DangbeiRuntimeData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DangbeiTV(entry, runtime)])


class DangbeiTV(CoordinatorEntity[bool], MediaPlayerEntity):
    """Media player entity (device_class=tv) for HomeKit / iOS remote control."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "tv"
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, runtime: DangbeiRuntimeData) -> None:
        super().__init__(runtime.projector_coordinator)
        self._entry_id = entry.entry_id
        self._client: DangbeiClient = runtime.client
        self._wol_client: DangbeiWolClient | None = runtime.wol_client
        self._coordinator = runtime.projector_coordinator
        self._bluetooth_mac: str = (
            {**entry.data, **entry.options}
        ).get(CONF_BLUETOOTH_MAC, "")
        self._attr_unique_id = f"{entry.entry_id}_tv"
        self._attr_device_info = projector_device_info(entry)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        return SUPPORT_DANGBEI_TV

    @property
    def state(self) -> str:
        """Projector power reflected by the coordinator's effective state."""
        return STATE_ON if self._coordinator.effective_state else STATE_OFF

    async def async_added_to_hass(self) -> None:
        """Listen for iOS remote direction-key events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(
                "homekit_tv_remote_key_pressed", self._handle_remote_key
            )
        )

    @callback
    def _handle_remote_key(self, event) -> None:
        """Route iOS remote direction keys to the matching Dangbei button entity."""
        if event.data.get("entity_id") != self.entity_id:
            return
        cmd = REMOTE_KEY_TO_CMD.get(event.data.get("key_name"))
        if cmd:
            self.hass.async_create_task(self._press_command(cmd))

    # ── 电源：操作 remote 实体（remote.dangbei_xxx）的开关 ──────────────────
    async def async_turn_on(self, **kwargs: Any) -> None:
        remote_entity = self._entity_id_by_unique(f"{self._entry_id}_remote", "remote")
        if remote_entity:
            await self.hass.services.async_call(
                "remote", "turn_on", {"entity_id": remote_entity}
            )
            return
        # 回退：remote 实体未注册时直接走 WOL
        if self._wol_client is None:
            raise HomeAssistantError(
                "Power-on requires a configured ESP32 wake-up device."
            )
        await self._wol_client.async_wakeup(bluetooth_mac=self._bluetooth_mac or None)
        await self._coordinator.async_begin_transition(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        remote_entity = self._entity_id_by_unique(f"{self._entry_id}_remote", "remote")
        if remote_entity:
            await self.hass.services.async_call(
                "remote", "turn_off", {"entity_id": remote_entity}
            )
            return
        # 回退：直接发关机命令
        await self._client.async_send_command(CMD_POWER_OFF)
        await self._coordinator.async_begin_transition(False)

    # ── iOS 播放 / 暂停 键 -> 当贝 home 主页 ────────────────────────────────
    # （HomeKit 的播放暂停键会被翻译成 media_player 服务调用，见 xiaomi_tv 的分析）
    async def async_media_play(self) -> None:
        await self._press_command(CMD_HOME)

    async def async_media_pause(self) -> None:
        await self._press_command(CMD_HOME)

    async def async_media_play_pause(self) -> None:
        await self._press_command(CMD_HOME)

    # ── 音量 ────────────────────────────────────────────────────────────────
    async def async_volume_up(self) -> None:
        await self._press_command(CMD_VOLUME_UP)

    async def async_volume_down(self) -> None:
        await self._press_command(CMD_VOLUME_DOWN)

    # ── 触发对应按钮实体 ────────────────────────────────────────────────────
    async def _press_command(self, command: str) -> None:
        """Trigger the matching Dangbei button entity (e.g. button.xxx_up).

        Falls back to sending the raw projector command if the button entity
        is not registered yet.
        """
        button_entity = self._entity_id_by_unique(
            f"{self._entry_id}_{command}", "button"
        )
        if button_entity:
            await self.hass.services.async_call(
                "button", "press", {"entity_id": button_entity}
            )
        else:
            await self._client.async_send_command(command)

    def _entity_id_by_unique(self, unique_id: str, domain: str) -> str | None:
        """Find an entity id by its registry unique_id and domain prefix."""
        ent_reg = self.hass.helpers.entity_registry.async_get(self.hass)
        prefix = f"{domain}."
        for entry in ent_reg.entities.values():
            if entry.unique_id == unique_id and entry.entity_id.startswith(prefix):
                return entry.entity_id
        return None
