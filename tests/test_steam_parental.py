"""Tests for the Steam parental-controls POC.

The one that matters is `test_unknown_fields_survive_a_write`. Changing a
playtime window means sending the whole ParentalSettings message back, so if
the codec drops a field it does not recognise, a write silently erases part of
a child's configuration - most likely their allowed-games list.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The component package itself imports homeassistant; its `api` subpackage
# deliberately does not, so it is put on the path and imported on its own.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / 'custom_components' / 'steam_parental'))

from api import parental  # noqa: E402
from api import protobuf_mini as pb  # noqa: E402
from api import windows as win  # noqa: E402


# --- window masks -----------------------------------------------------------

def test_mask_matches_valve_unlimited_sentinel():
    # fmgmt.js compares against BigInt(0xffffffffffff) for "unlimited".
    assert win.ALL_DAY == 0xFFFFFFFFFFFF
    assert win.mask_for([('00:00', '24:00')]) == win.ALL_DAY


@pytest.mark.parametrize('spans, expected', [
    ([('06:00', '20:00')], 0x00FFFFFFF000),
    ([('06:00', '12:00')], 0x000000FFF000),
    ([('15:30', '20:30')], 0x01FF80000000),
    ([('00:00', '00:30')], 0b1),
    ([('23:30', '24:00')], 1 << 47),
])
def test_known_masks(spans, expected):
    assert win.mask_for(spans) == expected


@pytest.mark.parametrize('spans', [
    [('06:00', '20:00')],
    [('07:00', '08:00'), ('15:30', '20:00')],
    [('00:00', '24:00')],
    [('00:00', '00:30'), ('23:30', '24:00')],
])
def test_spans_round_trip(spans):
    assert win.spans_of(win.mask_for(spans)) == spans


def test_adjacent_spans_merge_on_the_way_back():
    # Two touching spans are one run of bits; there is no way to tell them
    # apart afterwards, and no reason to want to.
    mask = win.mask_for([('06:00', '12:00'), ('12:00', '20:00')])
    assert win.spans_of(mask) == [('06:00', '20:00')]


def test_half_hour_resolution_is_enforced():
    with pytest.raises(ValueError, match='half-hour'):
        win.slot_of(8, 15)


def test_backwards_span_rejected():
    with pytest.raises(ValueError, match='ends before'):
        win.mask_for([('20:00', '06:00')])


def test_describe():
    assert win.describe(win.ALL_DAY) == 'all day'
    assert win.describe(0) == 'blocked'
    assert win.describe(win.mask_for([('06:00', '20:00')])) == '06:00-20:00'


# --- protobuf codec ---------------------------------------------------------

@pytest.mark.parametrize('value', [0, 1, 127, 128, 300, 2 ** 32, 2 ** 48 - 1,
                                   2 ** 63 - 1])
def test_varint_round_trip(value):
    fields = pb.decode(pb.encode([pb.varint(1, value)]))
    assert fields[0].value == value


def test_fixed64_round_trip():
    steamid = 76561197975368156
    fields = pb.decode(pb.encode([pb.fixed64(10, steamid)]))
    assert pb.read_fixed64(fields[0]) == steamid


def test_decode_encode_is_byte_identical():
    original = pb.encode([
        pb.varint(9, 1),
        pb.string(11, 'someone@example.com'),
        pb.message(15, [pb.varint(2, 1)]),
        pb.Field(7, pb.WIRE_FIXED32, b'\x01\x02\x03\x04'),
    ])
    assert pb.encode(pb.decode(original)) == original


def test_repeated_fields_keep_their_order():
    original = pb.encode([pb.message(15, [pb.varint(1, n)]) for n in range(7)])
    decoded = pb.decode(original)
    assert [pb.submessage(f)[0].value for f in decoded] == list(range(7))


# --- read/modify/write ------------------------------------------------------

def _settings_with_unknowns() -> list[pb.Field]:
    """A settings message holding fields this code deliberately does not model.

    Field 4 stands in for applist_base, 5 for applist_custom, 19 for
    utility_appids, and 99 for something Valve has not shipped yet.
    """
    return [
        pb.fixed64(parental.F_STEAMID, 76561197975368156),
        pb.message(4, [pb.varint(1, 440), pb.varint(2, 1)]),
        pb.message(5, [pb.varint(1, 570), pb.varint(2, 0)]),
        pb.Field(parental.F_SALT, pb.WIRE_LEN, b'\xde\xad\xbe\xef'),
        pb.Field(parental.F_PASSWORDHASH, pb.WIRE_LEN, b'\x01' * 32),
        pb.varint(parental.F_IS_ENABLED, 1),
        pb.varint(parental.F_ENABLED_FEATURES, 0b1010),
        pb.varint(19, 7),
        pb.varint(99, 12345),
    ]


def test_unknown_fields_survive_a_write():
    before = _settings_with_unknowns()
    days = [parental.Day(win.mask_for([('06:00', '20:00')]), 120)
            for _ in range(7)]

    after = parental.write_days(before, True, days)

    # Everything except the playtime block comes through byte for byte.
    def without_playtime(fields):
        return pb.encode([f for f in fields
                          if f.number != parental.F_PLAYTIME_RESTRICTIONS])

    assert without_playtime(after) == without_playtime(before)
    assert pb.get(after, 99) is not None
    assert pb.get(after, 4) is not None


def test_write_then_read_round_trips():
    days = [
        parental.Day(win.mask_for([('06:00', '20:00')]), 120),
        parental.Day(win.mask_for([('15:30', '20:00')]), 90),
        parental.Day(0, 0),
        parental.Day(win.ALL_DAY, win.UNLIMITED_MINUTES),
        parental.Day(win.mask_for([('07:00', '07:30')]), 30),
        parental.Day(win.mask_for([('06:00', '12:00')]), 60),
        parental.Day(win.ALL_DAY, 240),
    ]
    settings = parental.write_days(_settings_with_unknowns(), True, days)
    applied, read_back = parental.read_days(settings)

    assert applied is True
    assert read_back == days


def test_read_days_pads_a_short_week():
    settings = parental.write_days(_settings_with_unknowns(), True,
                                   [parental.Day() for _ in range(7)])
    # Rebuild with only three days, as a partially-configured account might be.
    holder = pb.get(settings, parental.F_PLAYTIME_RESTRICTIONS)
    inner = pb.submessage(holder)
    short = pb.replace_all(inner, parental.F_PLAYTIME_DAYS, [
        pb.message(parental.F_PLAYTIME_DAYS, [pb.varint(1, win.ALL_DAY)])
        for _ in range(3)
    ])
    settings = pb.replace(settings, parental.F_PLAYTIME_RESTRICTIONS,
                          pb.message(parental.F_PLAYTIME_RESTRICTIONS, short))

    _, days = parental.read_days(settings)
    assert len(days) == 7
    assert days[0].windows == win.ALL_DAY
    assert days[6].windows == 0


def test_read_days_on_an_account_with_no_restrictions():
    applied, days = parental.read_days([pb.varint(9, 1)])
    assert applied is False
    assert len(days) == 7


def test_write_days_rejects_a_short_week():
    with pytest.raises(ValueError, match='exactly 7 days'):
        parental.write_days([], True, [parental.Day()])


def test_write_days_rejects_an_oversized_mask():
    days = [parental.Day() for _ in range(7)]
    days[0] = parental.Day(1 << 48, 0)
    with pytest.raises(ValueError, match='48 bits'):
        parental.write_days([], True, days)


def test_toggling_restrictions_off_keeps_the_windows():
    days = [parental.Day(win.mask_for([('06:00', '20:00')]), 120)
            for _ in range(7)]
    settings = parental.write_days(_settings_with_unknowns(), False, days)
    applied, read_back = parental.read_days(settings)
    assert applied is False
    assert read_back == days


# --- redaction --------------------------------------------------------------

def test_dump_withholds_password_material():
    settings = _settings_with_unknowns()
    settings.append(pb.string(parental.F_RECOVERY_EMAIL, 'parent@example.com'))
    text = parental.dump(settings)

    assert 'parent@example.com' not in text
    assert 'deadbeef' not in text
    assert '<redacted' in text
    # Non-secret fields still show.
    assert '9: varint 1' in text
