"""Constants for the Steam Families parental controls integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = 'steam_parental'

CONF_REFRESH_TOKEN = 'refresh_token'
CONF_STEAMID = 'steamid'
CONF_FAMILY_GROUPID = 'family_groupid'
CONF_PIN = 'pin'
CONF_SKIP_PIN = 'skip_pin'

# Steam is not going to change underneath us minute to minute, and every poll
# is one request per family member. Five minutes keeps the entities honest
# without hammering an undocumented endpoint.
UPDATE_INTERVAL = timedelta(minutes=5)

# Access tokens last ~24h. Renew well before the edge so a slow poll never
# runs with an expired one.
TOKEN_RENEW_MARGIN = timedelta(hours=2)

# Family group roles, from GetFamilyGroupForUser.
ROLE_ADULT = 1
ROLE_CHILD = 2

SERVICE_SET_WINDOW = 'set_window'
SERVICE_SET_DAILY_LIMIT = 'set_daily_limit'
SERVICE_GRANT_TIME = 'grant_time'

ATTR_STEAMID = 'steamid'
ATTR_DAYS = 'days'
ATTR_SPANS = 'spans'
ATTR_MINUTES = 'minutes'
ATTR_ENFORCE = 'enforce'
