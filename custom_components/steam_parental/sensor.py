"""Sensors: today's allowed window, and the daily cap."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.util.dt as dt_util

from .api import windows as win
from .const import DOMAIN
from .coordinator import SteamParentalCoordinator
from .entity import SteamParentalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SteamParentalCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for steamid, member in coordinator.data.members.items():
        if not member.is_child:
            continue
        entities.append(WindowSensor(coordinator, steamid))
        entities.append(DailyLimitSensor(coordinator, steamid))
    async_add_entities(entities)


class WindowSensor(SteamParentalEntity, SensorEntity):
    _attr_translation_key = 'window_today'
    _attr_icon = 'mdi:calendar-clock'

    def __init__(self, coordinator, steamid):
        super().__init__(coordinator, steamid, 'window_today')

    @property
    def native_value(self) -> str:
        return win.describe(self.member.today(dt_util.now()).windows)

    @property
    def extra_state_attributes(self) -> dict:
        now = dt_util.now()
        day = self.member.today(now)
        return {
            'steamid': str(self._steamid),
            'mask': str(day.windows),
            'day_index': (now.weekday() + 1) % 7,
            'enabled': self.member.enabled,
            'enforced': self.member.enforced,
            'week': {win.DAY_NAMES[i]: win.describe(d.windows)
                     for i, d in enumerate(self.member.days)},
        }


class DailyLimitSensor(SteamParentalEntity, SensorEntity):
    _attr_translation_key = 'daily_limit'
    _attr_icon = 'mdi:timer-outline'
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator, steamid):
        super().__init__(coordinator, steamid, 'daily_limit')

    @property
    def native_value(self) -> int:
        return self.member.today(dt_util.now()).daily_minutes

    @property
    def extra_state_attributes(self) -> dict:
        return {
            'unlimited': (self.member.today(dt_util.now()).daily_minutes
                          >= win.UNLIMITED_MINUTES),
            'week': {win.DAY_NAMES[i]: d.daily_minutes
                     for i, d in enumerate(self.member.days)},
        }
