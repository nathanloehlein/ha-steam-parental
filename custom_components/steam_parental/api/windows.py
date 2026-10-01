"""Steam parental playtime windows: the 48-bit half-hour mask.

Derived from Valve's own family-management bundle, `fmgmt.js`:

    const n = r ^ (BigInt(1) << BigInt(slotIndex));
    t.allowed_time_windows = n.toString();

and the grid that produces `slotIndex`:

    for (let e = 0; e < 48; e++)         // 48 selectable slots
    ...
    Ee()().startOf('day').add(Math.floor(e / 2), 'hours')   // label for slot e

So bit N covers the half hour starting at N * 30 minutes after local midnight.
Slot 0 is 00:00-00:30, slot 47 is 23:30-24:00. The approve-playtime dialog
compares against `BigInt(0xffffffffffff)` for "unlimited", which pins the width
at exactly 48 bits.

The value travels as a decimal *string*, not a JSON number, because 2^48
exceeds what a double holds exactly.

Days are a 7-element array. Valve renders them with `moment().day(s)`, so index
0 is Sunday and index 6 is Saturday. The day dropdown reorders only the display
according to the viewer's locale; the stored index is always Sunday-based.
"""

from __future__ import annotations

SLOTS_PER_DAY = 48
MINUTES_PER_SLOT = 30
ALL_DAY = (1 << SLOTS_PER_DAY) - 1  # 281474976710655 == 0xffffffffffff
BLOCKED = 0
UNLIMITED_MINUTES = 1440

# Index into ParentalPlaytimeRestrictions.playtime_days.
SUNDAY, MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY = range(7)
DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
             'Saturday']


def slot_of(hour: int, minute: int = 0) -> int:
    """The slot containing this local time. 24:00 yields 48, one past the end."""
    if not 0 <= hour <= 24 or not 0 <= minute < 60:
        raise ValueError(f'{hour}:{minute:02d} is not a time of day')
    total = hour * 60 + minute
    if total % MINUTES_PER_SLOT:
        raise ValueError(
            f'{hour}:{minute:02d} is not on a half-hour boundary; Steam only '
            f'has {MINUTES_PER_SLOT}-minute resolution'
        )
    return total // MINUTES_PER_SLOT


def mask_for(spans: list[tuple[str, str]]) -> int:
    """Build a mask from "HH:MM" spans. End is exclusive; "24:00" means midnight.

    >>> hex(mask_for([('06:00', '20:00')]))
    '0xfffffff000'
    """
    mask = 0
    for start, end in spans:
        lo = slot_of(*_parse(start))
        hi = slot_of(*_parse(end))
        if hi <= lo:
            raise ValueError(f'span {start}-{end} ends before it starts')
        mask |= (1 << hi) - (1 << lo)
    return mask


def spans_of(mask: int) -> list[tuple[str, str]]:
    """The inverse of mask_for: contiguous runs as "HH:MM" pairs."""
    if not 0 <= mask <= ALL_DAY:
        raise ValueError(f'{mask} does not fit in {SLOTS_PER_DAY} bits')
    spans: list[tuple[str, str]] = []
    slot = 0
    while slot < SLOTS_PER_DAY:
        if mask >> slot & 1:
            start = slot
            while slot < SLOTS_PER_DAY and mask >> slot & 1:
                slot += 1
            spans.append((_format(start), _format(slot)))
        else:
            slot += 1
    return spans


def describe(mask: int) -> str:
    if mask == ALL_DAY:
        return 'all day'
    if mask == BLOCKED:
        return 'blocked'
    return ', '.join(f'{a}-{b}' for a, b in spans_of(mask))


def _parse(text: str) -> tuple[int, int]:
    hour, _, minute = text.partition(':')
    return int(hour), int(minute or 0)


def _format(slot: int) -> str:
    total = slot * MINUTES_PER_SLOT
    return f'{total // 60:02d}:{total % 60:02d}'
