"""Steam Families parental controls.

Reads and writes the playtime windows Steam enforces on child accounts,
including on a Steam Deck, which is the point: a Deck runs on battery, so
cutting power to a desk does nothing to it.

The services Steam exposes for this are undocumented. See STEAM.md for how the
wire format was established.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .api import parental
from .api import windows as win
from .const import (
    ATTR_DAYS,
    ATTR_ENFORCE,
    ATTR_MINUTES,
    ATTR_SPANS,
    ATTR_STEAMID,
    CONF_PIN,
    DOMAIN,
    SERVICE_GRANT_TIME,
    SERVICE_SET_DAILY_LIMIT,
    SERVICE_SET_WINDOW,
)
from .coordinator import SteamParentalCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]

DAY_NAMES = {name.lower(): index for index, name in enumerate(win.DAY_NAMES)}
DAY_NAMES.update({name.lower()[:3]: index
                  for index, name in enumerate(win.DAY_NAMES)})
DAY_GROUPS = {
    'all': list(range(7)),
    'weekdays': [win.MONDAY, win.TUESDAY, win.WEDNESDAY, win.THURSDAY,
                 win.FRIDAY],
    'weekend': [win.SATURDAY, win.SUNDAY],
    'today': [],  # resolved at call time
}

BASE_SCHEMA = {
    vol.Required(ATTR_STEAMID): vol.Coerce(int),
    vol.Optional(ATTR_DAYS, default='today'): cv.string,
    vol.Optional(ATTR_ENFORCE): cv.boolean,
}

SET_WINDOW_SCHEMA = vol.Schema({
    **BASE_SCHEMA,
    vol.Required(ATTR_SPANS): cv.string,
    vol.Optional(ATTR_MINUTES): vol.All(vol.Coerce(int), vol.Range(0, 1440)),
})

SET_DAILY_LIMIT_SCHEMA = vol.Schema({
    **BASE_SCHEMA,
    vol.Required(ATTR_MINUTES): vol.All(vol.Coerce(int), vol.Range(0, 1440)),
})

GRANT_TIME_SCHEMA = vol.Schema({
    vol.Required(ATTR_STEAMID): vol.Coerce(int),
    vol.Required(ATTR_MINUTES): vol.All(vol.Coerce(int), vol.Range(1, 1440)),
})


def resolve_days(text: str, hass: HomeAssistant) -> list[int]:
    """"today", "all", "weekdays", "weekend", or a comma-separated list."""
    cleaned = text.strip().lower()
    if cleaned == 'today':
        import homeassistant.util.dt as dt_util
        return [(dt_util.now().weekday() + 1) % 7]
    if cleaned in DAY_GROUPS:
        return DAY_GROUPS[cleaned]

    days = []
    for chunk in cleaned.split(','):
        key = chunk.strip()
        if key not in DAY_NAMES:
            raise HomeAssistantError(f'unknown day {chunk!r}')
        days.append(DAY_NAMES[key])
    if not days:
        raise HomeAssistantError('no days given')
    return days


def parse_spans(text: str) -> int:
    """"06:00-20:00[,21:00-22:00]", or the words all / none."""
    cleaned = text.strip().lower()
    if cleaned in ('all', 'allday', 'unlimited'):
        return win.ALL_DAY
    if cleaned in ('none', 'blocked', 'off'):
        return win.BLOCKED
    spans = []
    for chunk in cleaned.split(','):
        start, _, end = chunk.strip().partition('-')
        if not end:
            raise HomeAssistantError(
                f'span {chunk!r} needs a start and an end, like 06:00-20:00')
        spans.append((start.strip(), end.strip()))
    try:
        return win.mask_for(spans)
    except ValueError as err:
        raise HomeAssistantError(str(err)) from err


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = SteamParentalCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            for service in (SERVICE_SET_WINDOW, SERVICE_SET_DAILY_LIMIT,
                            SERVICE_GRANT_TIME):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


def _coordinator_for(hass: HomeAssistant, steamid: int) -> SteamParentalCoordinator:
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if coordinator.data and steamid in coordinator.data.members:
            return coordinator
    raise HomeAssistantError(f'{steamid} is not in any configured family')


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SET_WINDOW):
        return

    async def set_window(call: ServiceCall) -> None:
        steamid = call.data[ATTR_STEAMID]
        coordinator = _coordinator_for(hass, steamid)
        pin = coordinator.entry.data[CONF_PIN]
        mask = parse_spans(call.data[ATTR_SPANS])
        minutes = call.data.get(ATTR_MINUTES)

        member = coordinator.data.members[steamid]
        changes = {}
        for index in resolve_days(call.data[ATTR_DAYS], hass):
            current = member.days[index]
            changes[index] = parental.Day(
                windows=mask,
                daily_minutes=(current.daily_minutes if minutes is None
                               else minutes),
            )
        await coordinator.async_set_days(steamid, changes, pin,
                                         call.data.get(ATTR_ENFORCE))

    async def set_daily_limit(call: ServiceCall) -> None:
        steamid = call.data[ATTR_STEAMID]
        coordinator = _coordinator_for(hass, steamid)
        pin = coordinator.entry.data[CONF_PIN]
        member = coordinator.data.members[steamid]

        changes = {}
        for index in resolve_days(call.data[ATTR_DAYS], hass):
            changes[index] = parental.Day(
                windows=member.days[index].windows,
                daily_minutes=call.data[ATTR_MINUTES],
            )
        await coordinator.async_set_days(steamid, changes, pin,
                                         call.data.get(ATTR_ENFORCE))

    async def grant_time(call: ServiceCall) -> None:
        """Add minutes to today's cap. The chore-reward lever."""
        steamid = call.data[ATTR_STEAMID]
        coordinator = _coordinator_for(hass, steamid)
        pin = coordinator.entry.data[CONF_PIN]
        import homeassistant.util.dt as dt_util

        index = (dt_util.now().weekday() + 1) % 7
        current = coordinator.data.members[steamid].days[index]
        changes = {index: parental.Day(
            windows=current.windows,
            daily_minutes=min(win.UNLIMITED_MINUTES,
                              current.daily_minutes + call.data[ATTR_MINUTES]),
        )}
        await coordinator.async_set_days(steamid, changes, pin)

    hass.services.async_register(DOMAIN, SERVICE_SET_WINDOW, set_window,
                                 schema=SET_WINDOW_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SET_DAILY_LIMIT,
                                 set_daily_limit,
                                 schema=SET_DAILY_LIMIT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_GRANT_TIME, grant_time,
                                 schema=GRANT_TIME_SCHEMA)
