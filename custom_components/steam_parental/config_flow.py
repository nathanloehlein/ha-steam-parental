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

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import config_validation as cv

from .api import auth, parental
from .const import CONF_PIN, CONF_REFRESH_TOKEN, CONF_STEAMID, DOMAIN

_LOGGER = logging.getLogger(__name__)

# How long to leave the QR challenge up before giving up on it. Steam expires
# them on its own schedule; this just stops the task running forever.
APPROVAL_TIMEOUT = 300

# The QR code is drawn by the frontend, not here. Home Assistant's markdown
# sanitiser whitelists a `ha-qr-code` element with data / scale / margin /
# error-correction-level attributes, and the step description uses it.
#
# Worth knowing, because the obvious alternatives both fail: an inline SVG
# data URI is stripped unless the surrounding component opts into
# `allow-data-url`, and nothing in the frontend does, while raw inline SVG
# needs `allow-svg`, which nothing sets either.


class SteamParentalConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._challenge: auth.QRChallenge | None = None
        self._task: asyncio.Task[tuple[str, str]] | None = None
        self._refresh_token: str | None = None
        self._steamid: int | None = None
        self._account: str = ''

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
                description_placeholders={'url': self._challenge.url},
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
        self._abort_if_unique_id_configured()
        return self.async_show_progress_done(next_step_id='pin')

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
        """The parental PIN, needed for every write."""
        errors: dict[str, str] = {}

        if user_input is not None:
            assert self._refresh_token and self._steamid
            pin = user_input[CONF_PIN]
            valid = await self.hass.async_add_executor_job(
                self._check_pin, self._refresh_token, self._steamid, pin)
            if valid:
                return self.async_create_entry(
                    title=f'Steam Family ({self._account})',
                    data={
                        CONF_REFRESH_TOKEN: self._refresh_token,
                        CONF_STEAMID: self._steamid,
                        CONF_PIN: pin,
                    },
                )
            errors['base'] = 'invalid_pin'

        return self.async_show_form(
            step_id='pin',
            data_schema=vol.Schema({vol.Required(CONF_PIN): cv.string}),
            errors=errors,
        )

    @staticmethod
    def _check_pin(refresh_token: str, steamid: int, pin: str) -> bool:
        """Prove the PIN by writing the account's own settings back unchanged.

        There is no "validate this PIN" call that works for a family parent,
        so the check is a no-op write: read the settings, send exactly those
        back. A wrong PIN is refused, a right one changes nothing.
        """
        try:
            token = auth.access_token_from_refresh(refresh_token, steamid)
            api = parental.Client(token)
            settings = api.get_settings_raw(steamid)
            enforced, days = parental.read_days(settings)
            api.set_settings_raw(
                steamid, parental.write_days(settings, enforced, days),
                password=pin)
        except (auth.AuthError, parental.SteamError) as err:
            _LOGGER.debug('PIN check failed: %s', err)
            return False
        return True
