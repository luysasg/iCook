"""Pix "copia e cola" codes: found in iFood responses, shown as a QR code + copy button.

The MCP docs don't say where (or under which field) a Pix charge comes back, so we look
for the code itself in every string: a BR Code (EMV) starts with "000201", carries the
"br.gov.bcb.pix" domain and ends with "6304" + its CRC16, which we check.
"""

import re
from typing import Any

import segno

START = "000201"
DOMAIN = "br.gov.bcb.pix"
CRC_TAG = "6304"
QR_SCALE = 6
QR_BORDER = 2


def crc16(payload: str) -> str:
    """CRC16/CCITT-FALSE, as the BR Code spec requires (over everything up to and including "6304")."""
    crc = 0xFFFF
    for byte in payload.encode():
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
        crc &= 0xFFFF
    return f"{crc:04X}"


def code_in(text: str) -> str | None:
    """The first valid Pix code inside `text` (the merchant name/city in it may contain spaces)."""
    for start in (m.start() for m in re.finditer(START, text)):
        for end in (m.end() for m in re.finditer(CRC_TAG + "[0-9A-Fa-f]{4}", text[start:])):
            candidate = text[start : start + end]
            if DOMAIN in candidate.lower() and crc16(candidate[:-4]) == candidate[-4:].upper():
                return candidate
    return None


def find_code(obj: Any) -> str | None:
    """The first Pix code in any string of an MCP response."""
    if isinstance(obj, str):
        return code_in(obj)
    values = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else ()
    return next((code for code in map(find_code, values) if code), None)


def without_code(text: str) -> str:
    """The text with any Pix code taken out (the QR card shows it)."""
    code = code_in(text)
    return without_code(text.replace(code, "").strip()) if code else text


def card_view(code: str, total: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    qr = segno.make_qr(code, error="m").svg_data_uri(scale=QR_SCALE, border=QR_BORDER)
    return {"code": code, "qr": qr, "total": total, "dry_run": dry_run}


def _field(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def simulated_code(amount: float | None) -> str:
    """DRY_RUN: a well-formed code whose key ("icook-simulacao") no bank accepts."""
    payload = (
        _field("00", "01")
        + _field("26", _field("00", DOMAIN) + _field("01", "icook-simulacao"))
        + _field("52", "0000")
        + _field("53", "986")
        + (_field("54", f"{amount:.2f}") if amount else "")
        + _field("58", "BR")
        + _field("59", "ICOOK SIMULACAO")
        + _field("60", "SAO PAULO")
        + _field("62", _field("05", "***"))
        + CRC_TAG
    )
    return payload + crc16(payload)
