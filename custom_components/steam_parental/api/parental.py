"""Read and write Steam Families parental settings.

Two undocumented services are involved:

  IFamilyGroupsService   who is in the family. Plain JSON, scalar query params.
  IParentalService       the restrictions themselves. Protobuf.

Writing is a read-modify-write of the whole ParentalSettings message - there is
no granular setter - which is why protobuf_mini preserves unknown fields.

Field numbers come from SteamDatabase/Protobufs:
  steam/steammessages_parental.steamclient.proto
  steam/steammessages_parental_objects.proto
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import protobuf_mini as pb
from . import windows as win

API = 'https://api.steampowered.com'

# --- ParentalSettings -------------------------------------------------------
F_STEAMID = 1
F_PASSWORDHASHTYPE = 6
F_SALT = 7
F_PASSWORDHASH = 8
F_IS_ENABLED = 9
F_ENABLED_FEATURES = 10
F_RECOVERY_EMAIL = 11
F_PLAYTIME_RESTRICTIONS = 15

# Fields that must never be printed or logged.
SECRET_FIELDS = {F_SALT, F_PASSWORDHASH, F_RECOVERY_EMAIL}

# --- ParentalPlaytimeRestrictions -------------------------------------------
F_APPLY_RESTRICTIONS = 2
F_PLAYTIME_DAYS = 15

# --- ParentalPlaytimeDay ----------------------------------------------------
F_ALLOWED_TIME_WINDOWS = 1
F_ALLOWED_DAILY_MINUTES = 2

# --- request envelopes ------------------------------------------------------
F_REQ_PASSWORD = 1
F_REQ_SETTINGS = 2
F_REQ_NEW_PASSWORD = 3
F_REQ_SESSIONID = 4
F_REQ_STEAMID = 10


class SteamError(RuntimeError):
    """A non-2xx from the API, with whatever Steam said about it."""

    def __init__(self, status: int, message: str, eresult: str | None = None):
        self.status = status
        self.eresult = eresult
        detail = f'HTTP {status}'
        if eresult:
            detail += f' x-eresult={eresult}'
        super().__init__(f'{detail}: {message}')


@dataclass
class Day:
    """One day's restrictions. `windows` is the 48-bit half-hour mask."""

    windows: int = 0
    daily_minutes: int = 0

    def describe(self) -> str:
        minutes = ('unlimited' if self.daily_minutes >= win.UNLIMITED_MINUTES
                   else f'{self.daily_minutes} min')
        return f'{win.describe(self.windows):<40} cap {minutes}'


class Client:
    def __init__(self, access_token: str, timeout: int = 30):
        self._token = access_token
        self._timeout = timeout

    # -- transport -----------------------------------------------------------

    def _call(self, iface: str, method: str, *, body: bytes | None = None,
              params: dict[str, str] | None = None, as_json: bool = False,
              http: str = 'POST', version: int = 1) -> bytes | dict:
        """One unified-message call.

        Steam's WebAPI accepts a service method two ways. Scalar fields can go
        as ordinary query parameters with `format=json`, which is readable and
        enough for the simple requests. Anything with a nested message has to
        be base64 protobuf in `input_protobuf_encoded`.

        The HTTP verb is not ours to choose: Steam fixes it per method and
        answers 405 for the wrong one. Reads are GET, and a GET carries its
        protobuf in the query string rather than a body.
        """
        query = dict(params or {})
        query['access_token'] = self._token
        if as_json:
            query['format'] = 'json'

        data = None
        if body is not None:
            encoded = base64.b64encode(body).decode()
            if http == 'GET':
                query['input_protobuf_encoded'] = encoded
            else:
                data = urllib.parse.urlencode(
                    {'input_protobuf_encoded': encoded}
                ).encode()

        url = (f'{API}/{iface}/{method}/v{version}/?'
               + urllib.parse.urlencode(query))

        request = urllib.request.Request(url, data=data)
        request.add_header('User-Agent', 'home-assistant-steam-parental/0.1')
        if data is not None:
            request.add_header('Content-Type',
                               'application/x-www-form-urlencoded')
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            raise SteamError(
                err.code,
                err.read().decode('utf-8', 'replace')[:400] or err.reason,
                err.headers.get('x-eresult'),
            ) from None
        return json.loads(raw) if as_json else raw

    # -- family --------------------------------------------------------------

    def family_for_user(self) -> dict:
        """The token holder's family group: id, members, roles."""
        result = self._call(
            'IFamilyGroupsService', 'GetFamilyGroupForUser',
            params={'include_family_group_response': '1'}, as_json=True,
        )
        assert isinstance(result, dict)
        return result.get('response', result)

    def personas(self, steamids: list[int]) -> dict[int, str]:
        """Display names, so entities are not named after 17-digit numbers.

        ISteamUser/GetPlayerSummaries wants a publisher key and rejects a web
        token; the OAuth variant accepts one.
        """
        if not steamids:
            return {}
        result = self._call(
            'ISteamUserOAuth', 'GetUserSummaries', http='GET', as_json=True,
            version=2,
            params={'steamids': ','.join(str(s) for s in steamids)},
        )
        assert isinstance(result, dict)
        players = result.get('response', result).get('players', [])
        if isinstance(players, dict):
            players = players.get('player', [])
        return {int(p['steamid']): p.get('personaname', '')
                for p in players if p.get('steamid')}

    def get_requests(self, family_groupid: int) -> bytes:
        """Pending playtime and feature requests for the family."""
        body = pb.encode([pb.fixed64(2, family_groupid)])
        result = self._call('IParentalService', 'GetRequests', body=body,
                            http='GET')
        assert isinstance(result, bytes)
        return result

    # -- parental settings ---------------------------------------------------

    def get_settings_raw(self, steamid: int) -> list[pb.Field]:
        """The full ParentalSettings message, every field kept."""
        body = pb.encode([pb.fixed64(F_REQ_STEAMID, steamid)])
        raw = self._call('IParentalService', 'GetParentalSettings', body=body,
                         http='GET')
        assert isinstance(raw, bytes)
        response = pb.decode(raw)
        settings = pb.get(response, 1)
        if settings is None:
            raise SteamError(200, 'no settings in response - wrong steamid, '
                                  'or the token has no authority over it')
        return pb.submessage(settings)

    def set_settings_raw(self, steamid: int, settings: list[pb.Field],
                         password: str, sessionid: str = '') -> None:
        request = [
            pb.string(F_REQ_PASSWORD, password),
            pb.message(F_REQ_SETTINGS, settings),
        ]
        if sessionid:
            request.append(pb.string(F_REQ_SESSIONID, sessionid))
        request.append(pb.fixed64(F_REQ_STEAMID, steamid))
        self._call('IParentalService', 'SetParentalSettings',
                   body=pb.encode(request))


# --- playtime restrictions, as a 7-day list ---------------------------------

def read_days(settings: list[pb.Field]) -> tuple[bool, list[Day]]:
    """Pull the week out of a settings message. Always returns 7 days."""
    holder = pb.get(settings, F_PLAYTIME_RESTRICTIONS)
    if holder is None:
        return False, [Day() for _ in range(7)]
    restrictions = pb.submessage(holder)

    apply_field = pb.get(restrictions, F_APPLY_RESTRICTIONS)
    applied = bool(apply_field.value) if apply_field else False

    days: list[Day] = []
    for entry in pb.get_all(restrictions, F_PLAYTIME_DAYS):
        fields = pb.submessage(entry)
        mask = pb.get(fields, F_ALLOWED_TIME_WINDOWS)
        cap = pb.get(fields, F_ALLOWED_DAILY_MINUTES)
        days.append(Day(
            windows=int(mask.value) if mask else 0,
            daily_minutes=int(cap.value) if cap else 0,
        ))
    while len(days) < 7:
        days.append(Day())
    return applied, days[:7]


def write_days(settings: list[pb.Field], applied: bool,
               days: list[Day]) -> list[pb.Field]:
    """Return a copy of `settings` with the week replaced.

    Every other field, known or not, is carried through untouched - that is
    the whole point. Valve's own UI pads playtime_days to 7 entries, so this
    does too; a short array would leave later days unrestricted.
    """
    if len(days) != 7:
        raise ValueError(f'need exactly 7 days, got {len(days)}')

    day_fields = []
    for day in days:
        if not 0 <= day.windows <= win.ALL_DAY:
            raise ValueError(f'window mask {day.windows} exceeds 48 bits')
        if not 0 <= day.daily_minutes <= win.UNLIMITED_MINUTES:
            raise ValueError(f'daily minutes {day.daily_minutes} out of range')
        day_fields.append(pb.message(F_PLAYTIME_DAYS, [
            pb.varint(F_ALLOWED_TIME_WINDOWS, day.windows),
            pb.varint(F_ALLOWED_DAILY_MINUTES, day.daily_minutes),
        ]))

    existing = pb.get(settings, F_PLAYTIME_RESTRICTIONS)
    inner = pb.submessage(existing) if existing else []
    inner = pb.replace(inner, F_APPLY_RESTRICTIONS,
                       pb.varint(F_APPLY_RESTRICTIONS, 1 if applied else 0))
    inner = pb.replace_all(inner, F_PLAYTIME_DAYS, day_fields)

    return pb.replace(settings, F_PLAYTIME_RESTRICTIONS,
                      pb.message(F_PLAYTIME_RESTRICTIONS, inner))


def dump(settings: list[pb.Field]) -> str:
    """A readable dump with the password material and email withheld."""
    lines = []
    for field in settings:
        if field.number in SECRET_FIELDS:
            size = len(field.value) if isinstance(field.value, bytes) else 0
            lines.append(f'{field.number}: <redacted, {size} bytes>')
            continue
        lines.extend(pb.walk([field]))
    return '\n'.join(lines)
