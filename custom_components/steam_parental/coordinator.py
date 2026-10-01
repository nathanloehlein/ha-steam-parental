"""Polling and writes for Steam Families parental controls.

Everything the entities read comes through here. The Steam calls are blocking
`urllib`, so they run in the executor.

Token handling: the config entry stores a refresh token, which lasts months.
Access tokens last about a day and are minted on demand, a couple of hours
before the current one would lapse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import auth, parental
from .api import protobuf_mini as pb
from .api import windows as win
from .const import (
    CONF_PIN,
    CONF_REFRESH_TOKEN,
    CONF_STEAMID,
    DOMAIN,
    ROLE_CHILD,
    TOKEN_RENEW_MARGIN,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Member:
    """One family member's current parental state."""

    steamid: int
    name: str
    role: int
    enabled: bool = False
    enforced: bool = False
    days: list[parental.Day] = field(default_factory=list)

    @property
    def is_child(self) -> bool:
        return self.role == ROLE_CHILD

    def today(self, now: datetime) -> parental.Day:
        """Today's restrictions. Steam indexes days from Sunday."""
        return self.days[(now.weekday() + 1) % 7]

    def permitted_now(self, now: datetime) -> bool:
        """Whether the clock currently falls inside today's allowed window.

        Computed locally rather than asked of Steam, because no endpoint
        reports it and the mask is unambiguous. This says nothing about the
        daily minute cap, which only Steam knows the consumption of.
        """
        if not (self.enabled and self.enforced):
            return True
        slot = (now.hour * 60 + now.minute) // win.MINUTES_PER_SLOT
        return bool(self.today(now).windows >> slot & 1)


@dataclass
class SteamParentalData:
    family_groupid: int
    members: dict[int, Member]


class SteamParentalCoordinator(DataUpdateCoordinator[SteamParentalData]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN,
                         update_interval=UPDATE_INTERVAL)
        self.entry = entry
        self._refresh_token: str = entry.data[CONF_REFRESH_TOKEN]
        self._steamid: int = int(entry.data[CONF_STEAMID])
        self._client: parental.Client | None = None
        self._token_expires: datetime | None = None
        self._names: dict[int, str] = {}

    @property
    def pin(self) -> str:
        """The parental PIN, or empty if setup skipped it.

        Options win over data so it can be changed later without touching
        the refresh token, which is the part that needs a phone.
        """
        return (self.entry.options.get(CONF_PIN)
                or self.entry.data.get(CONF_PIN)
                or '')

    @property
    def can_write(self) -> bool:
        return bool(self.pin)

    # -- auth ----------------------------------------------------------------

    async def _api(self) -> parental.Client:
        """A client with a token good for at least the next couple of hours."""
        now = datetime.now(timezone.utc)
        fresh = (self._client is not None and self._token_expires is not None
                 and self._token_expires - now > TOKEN_RENEW_MARGIN)
        if fresh:
            assert self._client is not None
            return self._client

        try:
            token = await self.hass.async_add_executor_job(
                auth.access_token_from_refresh, self._refresh_token,
                self._steamid,
            )
        except auth.AuthError as err:
            # A dead refresh token is not a transient failure - it needs the
            # QR dance again, which only the user can do.
            raise ConfigEntryAuthFailed(str(err)) from err

        expires = auth.claims(token).get('exp')
        self._token_expires = (datetime.fromtimestamp(expires, timezone.utc)
                               if expires else None)
        self._client = parental.Client(token)
        return self._client

    # -- polling -------------------------------------------------------------

    async def _async_update_data(self) -> SteamParentalData:
        api = await self._api()
        try:
            return await self.hass.async_add_executor_job(self._fetch, api)
        except parental.SteamError as err:
            raise UpdateFailed(str(err)) from err

    def _fetch(self, api: parental.Client) -> SteamParentalData:
        group = api.family_for_user()
        family_groupid = int(group.get('family_groupid', 0))
        raw_members = group.get('family_group', {}).get('members', [])

        steamids = [int(m['steamid']) for m in raw_members]
        # Names change rarely; fetch once and keep them.
        missing = [s for s in steamids if s not in self._names]
        if missing:
            try:
                self._names.update(api.personas(missing))
            except parental.SteamError as err:
                _LOGGER.debug('persona lookup failed: %s', err)

        members: dict[int, Member] = {}
        for raw in raw_members:
            steamid = int(raw['steamid'])
            member = Member(
                steamid=steamid,
                name=self._names.get(steamid, str(steamid)),
                role=int(raw.get('role', 0)),
            )
            try:
                settings = api.get_settings_raw(steamid)
            except parental.SteamError as err:
                # One unreadable member should not blank the others.
                _LOGGER.warning('could not read settings for %s: %s',
                                steamid, err)
                member.days = [parental.Day() for _ in range(7)]
                members[steamid] = member
                continue

            enabled = pb.get(settings, parental.F_IS_ENABLED)
            member.enabled = bool(enabled.value) if enabled else False
            member.enforced, member.days = parental.read_days(settings)
            members[steamid] = member

        return SteamParentalData(family_groupid=family_groupid,
                                 members=members)

    # -- writes --------------------------------------------------------------

    async def async_set_days(self, steamid: int,
                             changes: dict[int, parental.Day],
                             enforce: bool | None = None) -> None:
        """Rewrite some days for one member, leaving the rest alone.

        Read-modify-write against a freshly fetched settings blob rather than
        the cached one: the whole message goes back to Steam, and a stale copy
        would undo anything changed in the Steam app since the last poll.
        """
        pin = self.pin
        if not pin:
            raise HomeAssistantError(
                'No Steam parental PIN is configured, so nothing can be '
                'changed. Add one in the integration options.')

        api = await self._api()

        def write() -> None:
            settings = api.get_settings_raw(steamid)
            enforced, days = parental.read_days(settings)
            for index, day in changes.items():
                if not 0 <= index <= 6:
                    raise HomeAssistantError(f'day index {index} out of range')
                days[index] = day
            updated = parental.write_days(
                settings, enforced if enforce is None else enforce, days)
            api.set_settings_raw(steamid, updated, password=pin)

        try:
            await self.hass.async_add_executor_job(write)
        except parental.SteamError as err:
            raise HomeAssistantError(f'Steam rejected the write: {err}') from err

        await self.async_request_refresh()
