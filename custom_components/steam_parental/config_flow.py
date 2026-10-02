"""Config flow: QR sign-in, then the parental PIN.

Sign-in is the Steam mobile app's QR approval rather than a username and
password. The integration never handles a Steam credential - it shows a link,
Steam authenticates the user on their phone, and the poll returns a refresh
token good for months.

Waiting is a progress step backed by a task, rather than a form submit that
blocks. Steam wants a poll every five seconds, and the person has to find
their phone, so the wait is better measured in minutes than in seconds; an
executor thread held open for all of it would be rude to everything else
sharing the pool.

The parental PIN is a different thing and unavoidable: Steam requires it on
every write. It is stored in the config entry like any other password.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .api import auth, parental
from .const import (
    CONF_PIN,
    CONF_REFRESH_TOKEN,
    CONF_SKIP_PIN,
    CONF_STEAMID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# How long to leave the QR challenge up before giving up on it. Steam expires
# them on its own schedule; this just stops the task running forever.
APPROVAL_TIMEOUT = 300

# The QR code is drawn by the frontend. Home Assistant's markdown sanitiser
# whitelists a `ha-qr-code` element taking data / scale / margin /
# error-correction-level, so the step description only has to emit one.
#
# The element is built here rather than written into strings.json because
# hassfest rejects HTML in translation strings. A placeholder is a runtime
# value and is substituted before the markdown is rendered, so it arrives at
# the sanitiser all the same.
#
# Worth recording, because the obvious alternatives both fail silently: an
# inline SVG data URI is stripped unless the surrounding component sets
# `allow-data-url`, and nothing in the frontend does, while raw inline SVG
# needs `allow-svg`, which is equally unset.


def _qr_element(url: str) -> str:
    """The challenge as a frontend-rendered QR code."""
    return (f'<ha-qr-code data="{url}" scale="6" '
            f'error-correction-level="medium"></ha-qr-code>')


class SteamParentalConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None
        self._challenge: auth.QRChallenge | None = None
        self._task: asyncio.Task[tuple[str, str]] | None = None
        self._refresh_token: str | None = None
        self._steamid: int | None = None
        self._account: str = ''

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Steam has stopped accepting the stored token.

        Most often because the password was changed, which revokes every
        refresh token on the account. The way back is the same QR scan as
        first setup, so this joins that flow and only differs at the end:
        the existing entry is updated in place, keeping the PIN, rather than
        a second entry being created.
        """
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context['entry_id'])
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the sign-in link, then hand over to the progress step."""
        if self._challenge is None:
            try:
                self._challenge = await self.hass.async_add_executor_job(
                    auth.begin_qr, 'home-assistant')
            except auth.AuthError as err:
                _LOGGER.error('could not start a Steam sign-in: %s', err)
                return self.async_abort(reason='cannot_connect')

        if user_input is None:
            return self.async_show_form(
                step_id='user',
                data_schema=vol.Schema({}),
                description_placeholders={
                    'url': self._challenge.url,
                    'qr': _qr_element(self._challenge.url),
                },
            )

        return await self.async_step_wait()

    async def async_step_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for the phone, without holding anything up."""
        assert self._challenge is not None

        if self._task is None:
            self._task = self.hass.async_create_task(
                self._await_approval(self._challenge))

        if not self._task.done():
            return self.async_show_progress(
                step_id='wait',
                progress_action='awaiting_approval',
                progress_task=self._task,
            )

        try:
            self._refresh_token, self._account = self._task.result()
        except auth.AuthError as err:
            _LOGGER.debug('QR sign-in did not complete: %s', err)
            # Steam will not reuse a stale challenge, so start over.
            self._challenge = None
            self._task = None
            return self.async_show_progress_done(next_step_id='retry')

        steamid = auth.claims(self._refresh_token).get('sub')
        if not steamid:
            return self.async_abort(reason='no_steamid')
        self._steamid = int(steamid)

        await self.async_set_unique_id(str(steamid))
        if self._reauth_entry is None:
            self._abort_if_unique_id_configured()
        elif self._reauth_entry.unique_id != str(steamid):
            # Signing in as somebody else would quietly repoint the entry at
            # a different family.
            return self.async_abort(reason='wrong_account')
        return self.async_show_progress_done(
            next_step_id='finish' if self._reauth_entry else 'pin')

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reauth only: swap in the new token and keep everything else."""
        assert self._reauth_entry and self._refresh_token and self._steamid
        return self.async_update_reload_and_abort(
            self._reauth_entry,
            data={
                **self._reauth_entry.data,
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_STEAMID: self._steamid,
            },
        )

    async def async_step_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nobody approved it. Offer a fresh link."""
        if user_input is None:
            return self.async_show_form(
                step_id='retry',
                data_schema=vol.Schema({}),
                errors={'base': 'not_approved'},
            )
        return await self.async_step_user()

    async def _await_approval(self, challenge: auth.QRChallenge) -> tuple[str, str]:
        """Poll Steam until the sign-in is approved.

        Each poll is a short executor call; the waiting between them is
        ordinary asyncio sleep, so no thread sits idle holding the line.
        """
        async with asyncio.timeout(APPROVAL_TIMEOUT):
            while True:
                result = await self.hass.async_add_executor_job(
                    auth.poll_once, challenge)
                if result is not None:
                    return result
                await asyncio.sleep(challenge.interval)

    async def async_step_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The parental PIN. Optional, because reading does not need it.

        Steam only asks for the PIN on a write. Everything that reports -
        the windows, the daily caps, whether play is permitted right now -
        works without it, so an entry with no PIN is useful rather than
        broken, and refusing to finish setup over it would be obnoxious.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            pin = (user_input.get(CONF_PIN) or '').strip()
            later = user_input.get(CONF_SKIP_PIN, False)

            if not pin and not later:
                errors['base'] = 'pin_required'
            elif not pin:
                return self._entry(pin='')
            else:
                assert self._refresh_token and self._steamid
                problem = await self.hass.async_add_executor_job(
                    self._check_pin, self._refresh_token, self._steamid, pin)
                if problem is None:
                    return self._entry(pin=pin)
                errors['base'] = problem

        return self.async_show_form(
            step_id='pin',
            data_schema=vol.Schema({
                vol.Optional(CONF_PIN, default=''): cv.string,
                vol.Required(CONF_SKIP_PIN, default=False): cv.boolean,
            }),
            errors=errors,
        )

    def _entry(self, pin: str) -> ConfigFlowResult:
        assert self._refresh_token and self._steamid
        return self.async_create_entry(
            title=f'Steam Family ({self._account})',
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_STEAMID: self._steamid,
                CONF_PIN: pin,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return SteamParentalOptionsFlow()

    @staticmethod
    def _check_pin(refresh_token: str, steamid: int, pin: str) -> str | None:
        """Prove the PIN by writing the account's own settings back unchanged.

        There is no "validate this PIN" call that works for a family parent,
        so the check is a no-op write: read the settings, send exactly those
        back. A wrong PIN is refused, a right one changes nothing.

        Returns None when the PIN is good, otherwise the error key to show.
        Distinguishing the cases matters: a token or network problem looks
        nothing like a wrong PIN to the person typing it, and reporting
        everything as "invalid PIN" sends them off hunting the wrong thing.
        """
        try:
            token = auth.access_token_from_refresh(refresh_token, steamid)
        except auth.AuthError as err:
            _LOGGER.error('Steam sign-in token could not be renewed: %s', err)
            return 'auth_failed'

        claims = auth.claims(token)
        _LOGGER.debug('minted access token, audience %s', claims.get('aud'))

        api = parental.Client(token)
        try:
            settings = api.get_settings_raw(steamid)
        except parental.SteamError as err:
            _LOGGER.error(
                'could not read parental settings for %s (audience %s): %s',
                steamid, claims.get('aud'), err)
            return 'cannot_read'

        try:
            enforced, days = parental.read_days(settings)
            api.set_settings_raw(
                steamid, parental.write_days(settings, enforced, days),
                password=pin)
        except parental.SteamError as err:
            # EResult 5 is InvalidPassword, 15 AccessDenied. Anything else is
            # not the PIN, whatever the dialog would like to blame.
            if err.eresult in ('5', '15') or err.status in (401, 403):
                _LOGGER.warning('Steam refused the parental PIN: %s', err)
                return 'invalid_pin'
            _LOGGER.error(
                'parental write failed, and not because of the PIN '
                '(audience %s): %s', claims.get('aud'), err)
            return 'write_failed'
        return None


class SteamParentalOptionsFlow(OptionsFlow):
    """Set or change the parental PIN without signing in again.

    The refresh token is the expensive part of setup - it needs a phone and a
    QR scan - so changing the PIN must not disturb it.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        current = entry.options.get(CONF_PIN, entry.data.get(CONF_PIN, ''))

        if user_input is not None:
            pin = (user_input.get(CONF_PIN) or '').strip()
            if not pin:
                # Clearing it is allowed: it puts the entry back to
                # read-only, which beats leaving a PIN that no longer works.
                return self.async_create_entry(data={CONF_PIN: ''})

            problem = await self.hass.async_add_executor_job(
                SteamParentalConfigFlow._check_pin,
                entry.data[CONF_REFRESH_TOKEN],
                int(entry.data[CONF_STEAMID]),
                pin,
            )
            if problem is None:
                return self.async_create_entry(data={CONF_PIN: pin})
            errors['base'] = problem

        return self.async_show_form(
            step_id='init',
            data_schema=vol.Schema({
                vol.Optional(CONF_PIN, default=current): cv.string,
            }),
            errors=errors,
        )
