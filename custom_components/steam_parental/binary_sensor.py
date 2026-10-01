"""Whether the clock currently falls inside the allowed window.

Computed locally. Steam has no endpoint that reports it, and the mask leaves
no room for interpretation. It does not account for the daily minute cap,
whose consumption only Steam tracks - so this answers "is play allowed at this
hour", not "has the budget run out".
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .coordinator import SteamParentalCoordinator
from .entity import SteamParentalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SteamParentalCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        PlaytimePermitted(coordinator, steamid)
        for steamid, member in coordinator.data.members.items()
        if member.is_child
    )


class PlaytimePermitted(SteamParentalEntity, BinarySensorEntity):
    _attr_translation_key = 'playtime_permitted'
    _attr_icon = 'mdi:gamepad-variant'

    def __init__(self, coordinator, steamid):
        super().__init__(coordinator, steamid, 'playtime_permitted')

    @property
    def is_on(self) -> bool:
        return self.member.permitted_now(dt_util.now())

    @property
    def extra_state_attributes(self) -> dict:
        return {
            'reason': ('no restrictions in force'
                       if not (self.member.enabled and self.member.enforced)
                       else 'inside window' if self.is_on
                       else 'outside window'),
        }
