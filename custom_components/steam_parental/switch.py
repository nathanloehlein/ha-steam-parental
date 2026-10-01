"""The enforcement switch.

`apply_playtime_restrictions` is a separate flag from the windows themselves.
Windows can sit stored and idle, which is how both of this family's child
accounts were found. This switch is that flag, and nothing else - flipping it
does not alter a single window.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SteamParentalCoordinator
from .entity import SteamParentalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SteamParentalCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EnforcementSwitch(coordinator, steamid)
        for steamid, member in coordinator.data.members.items()
        if member.is_child
    )


class EnforcementSwitch(SteamParentalEntity, SwitchEntity):
    _attr_translation_key = 'playtime_enforced'
    _attr_icon = 'mdi:shield-clock'

    def __init__(self, coordinator, steamid):
        super().__init__(coordinator, steamid, 'playtime_enforced')

    @property
    def is_on(self) -> bool:
        return self.member.enforced

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    @property
    def available(self) -> bool:
        # Without a PIN this is a read-only mirror of a setting nobody here
        # can move, and a switch that silently refuses is worse than one that
        # shows itself as unavailable.
        return super().available and self.coordinator.can_write

    async def _set(self, enforce: bool) -> None:
        # No day changes; only the flag moves.
        await self.coordinator.async_set_days(self._steamid, {},
                                              enforce=enforce)
