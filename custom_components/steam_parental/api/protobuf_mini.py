"""A tiny protobuf codec that preserves fields it does not understand.

Why hand-roll instead of using the `protobuf` package: this has to be able to
run inside the Home Assistant container, which has a bare Python and no pip
install step we control. The messages involved are small, and only a couple of
fields are ever touched.

The important property is round-trip fidelity. `ParentalSettings` carries the
whole of a Steam account's parental configuration - the app allowlist, the
password hash, the content descriptors - and the only way to change the
playtime windows is to send the entire message back. Decoding into a known
schema and re-encoding would silently drop anything the schema missed, which
on this message means quietly wiping a child's allowed-games list.

So a message decodes to an ordered list of raw fields. Unknown fields keep
their bytes and their position; only the field being edited is rebuilt.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5


@dataclass
class Field:
    """One wire-level field: its number, its wire type, and its raw value.

    `value` is an int for varints, and bytes for everything else. Length
    prefixes are not included - they are regenerated on encode.
    """

    number: int
    wire_type: int
    value: int | bytes


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError('truncated varint')
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError('varint too long')


def _write_varint(value: int) -> bytes:
    if value < 0:
        # Negative ints are two's-complement 64-bit on the wire. Nothing here
        # uses signed fields, but failing loudly beats emitting garbage.
        raise ValueError('negative varint not supported')
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _take(buf: bytes, pos: int, count: int) -> tuple[bytes, int]:
    """Slice exactly `count` bytes, or fail.

    Slicing past the end of a bytes object quietly returns something shorter,
    which lets a truncated or misread buffer decode into plausible-looking
    nonsense. The dump helper relies on a bad guess raising here.
    """
    end = pos + count
    if count < 0 or end > len(buf):
        raise ValueError(f'need {count} bytes at {pos}, have {len(buf) - pos}')
    return buf[pos:end], end


def decode(buf: bytes) -> list[Field]:
    """Decode a message into its wire fields, in the order they appeared."""
    fields: list[Field] = []
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        number, wire_type = key >> 3, key & 0x07
        if wire_type == WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire_type == WIRE_FIXED64:
            value, pos = _take(buf, pos, 8)
        elif wire_type == WIRE_LEN:
            length, pos = _read_varint(buf, pos)
            value, pos = _take(buf, pos, length)
        elif wire_type == WIRE_FIXED32:
            value, pos = _take(buf, pos, 4)
        else:
            raise ValueError(f'unsupported wire type {wire_type}')
        fields.append(Field(number, wire_type, value))
    return fields


def encode(fields: list[Field]) -> bytes:
    out = bytearray()
    for field in fields:
        out += _write_varint((field.number << 3) | field.wire_type)
        if field.wire_type == WIRE_VARINT:
            assert isinstance(field.value, int)
            out += _write_varint(field.value)
        elif field.wire_type == WIRE_LEN:
            assert isinstance(field.value, bytes)
            out += _write_varint(len(field.value))
            out += field.value
        else:
            assert isinstance(field.value, bytes)
            out += field.value
    return bytes(out)


def get(fields: list[Field], number: int) -> Field | None:
    """The last field with this number, or None. Last wins, as protobuf says."""
    found = None
    for field in fields:
        if field.number == number:
            found = field
    return found


def get_all(fields: list[Field], number: int) -> list[Field]:
    return [f for f in fields if f.number == number]


def replace(fields: list[Field], number: int, new: Field) -> list[Field]:
    """Swap a single field in place, keeping its position. Appends if absent."""
    out = []
    done = False
    for field in fields:
        if field.number == number and not done:
            out.append(new)
            done = True
        elif field.number == number:
            continue  # drop duplicates of a singular field
        else:
            out.append(field)
    if not done:
        out.append(new)
    return out


def replace_all(fields: list[Field], number: int, new: list[Field]) -> list[Field]:
    """Swap every field with this number for `new`, at the first one's position."""
    out: list[Field] = []
    inserted = False
    for field in fields:
        if field.number == number:
            if not inserted:
                out.extend(new)
                inserted = True
            continue
        out.append(field)
    if not inserted:
        out.extend(new)
    return out


def varint(number: int, value: int) -> Field:
    return Field(number, WIRE_VARINT, value)


def message(number: int, fields: list[Field]) -> Field:
    return Field(number, WIRE_LEN, encode(fields))


def string(number: int, value: str) -> Field:
    return Field(number, WIRE_LEN, value.encode('utf-8'))


def fixed64(number: int, value: int) -> Field:
    return Field(number, WIRE_FIXED64, struct.pack('<Q', value))


def read_fixed64(field: Field) -> int:
    assert isinstance(field.value, bytes)
    return struct.unpack('<Q', field.value)[0]


def submessage(field: Field) -> list[Field]:
    assert isinstance(field.value, bytes)
    return decode(field.value)


def walk(fields: list[Field], indent: int = 0) -> Iterator[str]:
    """Human-readable dump, guessing which length-delimited fields are nested."""
    for field in fields:
        pad = '  ' * indent
        if field.wire_type == WIRE_VARINT:
            yield f'{pad}{field.number}: varint {field.value}'
        elif field.wire_type == WIRE_FIXED64:
            yield f'{pad}{field.number}: fixed64 {read_fixed64(field)}'
        elif field.wire_type == WIRE_FIXED32:
            yield f'{pad}{field.number}: fixed32 {field.value.hex()}'
        else:
            assert isinstance(field.value, bytes)
            # A length-delimited field is a string, a nested message, or opaque
            # bytes, and the wire format does not say which. Printable text is
            # checked first: a short string will often also parse as a message,
            # and reading it as one produces convincing gibberish.
            text = _as_text(field.value)
            if text is not None:
                yield f'{pad}{field.number}: string {text!r}'
                continue
            nested = _try_decode(field.value)
            if nested is not None:
                yield f'{pad}{field.number}: message ({len(field.value)} bytes)'
                yield from walk(nested, indent + 1)
            else:
                yield f'{pad}{field.number}: bytes {_preview(field.value)}'


def _as_text(buf: bytes) -> str | None:
    """The bytes as a printable string, or None if they are not one."""
    if not buf:
        return None
    try:
        text = buf.decode('utf-8')
    except UnicodeDecodeError:
        return None
    return text if text.isprintable() else None


def _try_decode(buf: bytes) -> list[Field] | None:
    if not buf:
        return None
    try:
        fields = decode(buf)
    except (ValueError, IndexError):
        return None
    return fields or None


def _preview(buf: bytes) -> str:
    try:
        text = buf.decode('utf-8')
    except UnicodeDecodeError:
        return buf.hex() if len(buf) <= 32 else buf[:32].hex() + '...'
    return repr(text) if text.isprintable() else buf.hex()
