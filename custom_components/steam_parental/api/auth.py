"""Steam authentication, for a token that outlives the afternoon.

The `webapi_token` pulled out of a browser session lasts about 24 hours, which
is fine for probing and useless for anything that runs unattended. The durable
credential is a *refresh token*, good for roughly 200 days, which mints fresh
access tokens on demand.

Getting one without handling a password: `BeginAuthSessionViaQR` returns a
challenge URL, the Steam mobile app approves it, and `PollAuthSessionStatus`
hands back the refresh token. Nathan scans, Steam authenticates, nothing here
ever sees a credential.

  IAuthenticationService/BeginAuthSessionViaQR        POST, unauthenticated
  IAuthenticationService/PollAuthSessionStatus        POST, unauthenticated
  IAuthenticationService/GenerateAccessTokenForApp    POST, refresh token

The access token this produces has audience `web`, which is what
IParentalService wants.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import protobuf_mini as pb

API = 'https://api.steampowered.com'

# CAuthentication_BeginAuthSessionViaQR_Request
F_QR_DEVICE_FRIENDLY_NAME = 1
F_QR_PLATFORM_TYPE = 2
F_QR_DEVICE_DETAILS = 4

# CAuthentication_BeginAuthSessionViaQR_Response, read off a live response:
#   1: varint client_id
#   2: string challenge_url   https://s.team/q/1/<client_id>
#   3: bytes  request_id      16 bytes
#   4: float  interval        5.0
#   5: repeated allowed_confirmations
F_QR_RESP_CLIENT_ID = 1
F_QR_RESP_CHALLENGE_URL = 2
F_QR_RESP_REQUEST_ID = 3
F_QR_RESP_INTERVAL = 4

# CAuthentication_PollAuthSessionStatus_Request
F_POLL_CLIENT_ID = 1
F_POLL_REQUEST_ID = 2

# CAuthentication_PollAuthSessionStatus_Response
F_POLL_RESP_NEW_CLIENT_ID = 1
F_POLL_RESP_REFRESH_TOKEN = 3
F_POLL_RESP_ACCESS_TOKEN = 4
F_POLL_RESP_ACCOUNT_NAME = 6

# CAuthentication_AccessToken_GenerateForApp_Request
F_GEN_REFRESH_TOKEN = 1
F_GEN_STEAMID = 2
F_GEN_RENEWAL_TYPE = 3

# CAuthentication_AccessToken_GenerateForApp_Response
F_GEN_RESP_ACCESS_TOKEN = 1
F_GEN_RESP_REFRESH_TOKEN = 2

# EAuthTokenPlatformType
PLATFORM_WEB_BROWSER = 2


class AuthError(RuntimeError):
    pass


@dataclass
class QRChallenge:
    client_id: int
    request_id: bytes
    url: str
    interval: float


def _post(method: str, body: bytes, *, timeout: int = 30) -> bytes:
    url = f'{API}/IAuthenticationService/{method}/v1/'
    data = urllib.parse.urlencode(
        {'input_protobuf_encoded': base64.b64encode(body).decode()}
    ).encode()
    request = urllib.request.Request(url, data=data)
    request.add_header('Content-Type', 'application/x-www-form-urlencoded')
    request.add_header('User-Agent', 'home-assistant-steam-parental/0.1')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        raise AuthError(
            f'{method}: HTTP {err.code} '
            f'x-eresult={err.headers.get("x-eresult")} '
            f'{err.read().decode("utf-8", "replace")[:200]}'
        ) from None


def begin_qr(device_name: str = 'home-assistant') -> QRChallenge:
    body = pb.encode([
        pb.string(F_QR_DEVICE_FRIENDLY_NAME, device_name),
        pb.varint(F_QR_PLATFORM_TYPE, PLATFORM_WEB_BROWSER),
    ])
    fields = pb.decode(_post('BeginAuthSessionViaQR', body))

    client_id = pb.get(fields, F_QR_RESP_CLIENT_ID)
    challenge = pb.get(fields, F_QR_RESP_CHALLENGE_URL)
    request_id = pb.get(fields, F_QR_RESP_REQUEST_ID)
    interval = pb.get(fields, F_QR_RESP_INTERVAL)
    if not (client_id and challenge and request_id):
        raise AuthError('incomplete QR challenge from Steam')

    return QRChallenge(
        client_id=int(client_id.value),
        request_id=request_id.value,
        url=challenge.value.decode(),
        interval=_as_float(interval) if interval else 5.0,
    )


def poll_once(challenge: QRChallenge) -> tuple[str, str] | None:
    """One poll. Returns (refresh_token, account_name), or None if still waiting.

    Single-shot on purpose. Inside Home Assistant this runs in the executor,
    and a call that blocked a pooled thread for the whole three minutes
    somebody takes to find their phone would be rude to everything else
    sharing that pool.
    """
    body = pb.encode([
        pb.varint(F_POLL_CLIENT_ID, challenge.client_id),
        pb.Field(F_POLL_REQUEST_ID, pb.WIRE_LEN, challenge.request_id),
    ])
    fields = pb.decode(_post('PollAuthSessionStatus', body))
    refresh = pb.get(fields, F_POLL_RESP_REFRESH_TOKEN)
    if refresh and refresh.value:
        name = pb.get(fields, F_POLL_RESP_ACCOUNT_NAME)
        return refresh.value.decode(), name.value.decode() if name else ''
    return None


def poll_until_approved(challenge: QRChallenge, *,
                        deadline_seconds: int = 180) -> tuple[str, str]:
    """Block until the phone approves. For the CLI, not for Home Assistant."""
    give_up = time.monotonic() + deadline_seconds
    while time.monotonic() < give_up:
        result = poll_once(challenge)
        if result is not None:
            return result
        time.sleep(challenge.interval)
    raise AuthError('nobody approved the QR code in time')


def access_token_from_refresh(refresh_token: str, steamid: int) -> str:
    """Mint a fresh access token. This is the call HA makes on a schedule."""
    body = pb.encode([
        pb.string(F_GEN_REFRESH_TOKEN, refresh_token),
        pb.fixed64(F_GEN_STEAMID, steamid),
    ])
    fields = pb.decode(_post('GenerateAccessTokenForApp', body))
    token = pb.get(fields, F_GEN_RESP_ACCESS_TOKEN)
    if not token or not token.value:
        raise AuthError('Steam returned no access token; the refresh token '
                        'has probably expired or been revoked')
    return token.value.decode()


def claims(token: str) -> dict:
    """The JWT payload. Identity and expiry only."""
    parts = token.split('.')
    if len(parts) != 3:
        return {}
    payload = parts[1] + '=' * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


def _as_float(field: pb.Field) -> float:
    # `interval` is a protobuf float, so wire type 5, four bytes little-endian.
    import struct
    if field.wire_type == pb.WIRE_FIXED32:
        assert isinstance(field.value, bytes)
        return struct.unpack('<f', field.value)[0]
    return float(field.value) if isinstance(field.value, int) else 5.0
