"""Shared entity base: one device per family member."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import Member, SteamParentalCoordinator


class SteamParentalEntity(CoordinatorEntity[SteamParentalCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SteamParentalCoordinator,
                 steamid: int, key: str) -> None:
        super().__init__(coordinator)
        self._steamid = steamid
        self._attr_unique_id = f'{steamid}_{key}'
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(steamid))},
            name=self.member.name,
            manufacturer='Valve',
            model='Steam account',
            configuration_url=f'https://steamcommunity.com/profiles/{steamid}',
        )

    @property
    def member(self) -> Member:
        return self.coordinator.data.members[self._steamid]

    @property
    def available(self) -> bool:
        return (super().available and self.coordinator.data is not None
                and self._steamid in self.coordinator.data.members)
