# Steam Families Parental Controls for Home Assistant

Read and write the playtime windows Steam enforces on child accounts — from
Home Assistant, on a schedule, or off the back of anything else in your house.

Steam enforces these at the account level, so they apply to **every device the
account is signed into, including a Steam Deck**. That is the point: a Deck
runs on battery, so cutting power to a desk does nothing to it, and a network
block only stops the online half.

> Steam publishes no API for this. `IParentalService` is undocumented and, as
> far as a search of public code shows, unused — every reference to
> `SetParentalSettings` on GitHub is generated protobuf bindings with no
> callers. This integration was built by reverse-engineering Valve's own
> family-management bundle. It works, and Valve can break it whenever they
> like.

## What you get

Per child account in your Steam Family:

| Entity | What |
| --- | --- |
| `sensor.<name>_allowed_hours_today` | today's window, e.g. `09:00-21:00`; the whole week in attributes |
| `sensor.<name>_daily_limit_today` | minutes-per-day cap |
| `binary_sensor.<name>_playtime_permitted` | whether the clock is inside today's window |
| `switch.<name>_playtime_enforced` | the enforcement flag, on its own |

And three services:

- `steam_parental.set_window` — replace the allowed hours for some days
- `steam_parental.set_daily_limit` — change the cap without touching the hours
- `steam_parental.grant_time` — add minutes to today's cap

## Two switches, easily confused

- `is_enabled` — parental settings on at all
- `apply_playtime_restrictions` — whether the windows actually bite

Windows can sit stored and completely idle. Check the `playtime_enforced`
switch before wondering why nothing happens.

## Installation

HACS → ⋮ → Custom repositories → add this repository as an Integration. Then
install, restart, and add the integration from Settings → Devices & Services.

Setup is two steps:

1. **Sign in.** A QR code appears. In the Steam mobile app, open the menu and
   choose *Sign in with QR code*, then scan it. Home Assistant never sees your
   Steam password — Steam authenticates you and returns a refresh token, good
   for months.
2. **Family View PIN.** Steam requires it on every write. It is verified
   immediately by writing your own settings back unchanged.

## Example: tie Steam to chores

```yaml
automation:
  - alias: Steam follows the chore gate
    triggers:
      - trigger: state
        entity_id: binary_sensor.shared_screen_time_permitted
    actions:
      - action: steam_parental.set_window
        data:
          steamid: "7656119xxxxxxxxxx"
          days: today
          spans: >-
            {{ '09:00-21:00' if is_state(
                 'binary_sensor.shared_screen_time_permitted', 'on')
               else 'none' }}
```

Reward extra time rather than rewriting the schedule:

```yaml
      - action: steam_parental.grant_time
        data:
          steamid: "7656119xxxxxxxxxx"
          minutes: 30
```

## Resolution and timing

Windows have **half-hour** resolution — that is Steam's limit, not this
integration's. `08:15` is rejected; `08:30` is fine.

How quickly a running Deck notices a changed window is not established.
Steam's protocol has a settings-change notification, which implies push, but
it has not been measured here. Assume next launch until proven otherwise.

## How changes are written safely

There is no granular setter. Changing one window means sending Steam the
entire `ParentalSettings` message back, which on a real account carries the
per-game allowlist, content-descriptor exclusions and utility app IDs — dozens
of entries this integration has no business understanding.

So the protobuf codec decodes to raw wire fields and rebuilds only the field
being edited. Everything else returns byte for byte. There is a test that
asserts exactly this, and it is the most important one in the suite.

Writes also re-read the settings immediately before changing them, rather than
using the cached poll, so a change made in the Steam app a minute ago is not
quietly reverted.

## How the wire format was worked out

`allowed_time_windows` is a 48-bit mask, one bit per half hour of local time.
Bit 0 is 00:00-00:30, bit 47 is 23:30-24:00. That comes from Valve's own
family-management bundle, `fmgmt.js`:

```js
const n = r ^ (BigInt(1) << BigInt(slotIndex));
t.allowed_time_windows = n.toString();
```

with the grid looping `for (let e = 0; e < 48; e++)` and labelling each slot
`startOf('day').add(Math.floor(e / 2), 'hours')`. The approve-playtime dialog
compares against `BigInt(0xffffffffffff)` for "unlimited", which pins the
width at exactly 48 bits. The value travels as a decimal *string*, because
2^48 does not fit a double exactly.

| Window | Decimal | Hex |
| --- | --- | --- |
| all day | 281474976710655 | `0xffffffffffff` |
| 06:00-20:00 | 1099511623680 | `0x00fffffff000` |
| blocked | 0 | `0x000000000000` |

`playtime_days` is a 7-element array indexed from **Sunday**. Valve builds it
with `moment().day(s)`; the day dropdown reorders only the display according
to the viewer's locale.

Message and field numbers come from
[SteamDatabase/Protobufs](https://github.com/SteamDatabase/Protobufs)
(`steammessages_parental.steamclient.proto` and
`steammessages_parental_objects.proto`). The QR sign-in response field numbers
were read off a live response, and differ from the obvious guess: client_id is
1, challenge_url 2, request_id 3, interval 4.

## Status

Working, and in use. The API layer is covered by tests and has been exercised
against a live family account: reads, writes, field preservation and PIN
survival are all verified. The Home Assistant side is newer and less
weathered. Issues and pull requests welcome.
