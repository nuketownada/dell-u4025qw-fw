#!/usr/bin/env python3
"""
.upg parser, unified schema (verified against U4025QW M3T105 + U3224KB M2T105).

The schema below was reverse-engineered from Dell's `Firmware Updater`
binary (FUN_001b3960 = read_string, FUN_001b3c40 = read_string_list,
FUN_001bfac0 = read_components), not guessed.

  HEADER (4 strings):
    string  magic              ("UPG")
    string  format_version     ("1.0.5" / "1.0.6")
    string  product            ("U4025QW", "Dell U3224KB", ...)
    string  fw_version         ("M3T105", "M2T105", ...)

  LISTS (2 string-lists):
    string_list  name_table    declared component names
    string_list  panel_bound   subset of names that have a second
                               metadata entry keyed by an encrypted
                               panel_id (used for panel-firmware binding)

  COMPONENTS:
    u32   N        = name_table.size() + panel_bound.size()
    for each of N components:
      string      key       plaintext name (e.g. "HUB") OR
                            encrypted-Base64URL panel_id for the
                            panel-bound copy of a component
      string[17]  fields    17 Base64URL-encrypted metadata fields,
                            each decryptable with `Wistron@<product>`

  BINARY:
    u32   M        = N
    for each of M binary entries:
      string      key       matches one component's `key`
      bytes       payload   ECIES-secp521r1 + Gunzip + HexDecode
                            (decryptable with the static CK_PV key
                             baked into every shipped libhub.so)

  Optional TBT trailer follows for monitors with Thunderbolt — not
  parsed here.

Encoding primitives (everything uses big-endian length prefixes):
  string      = u32 BE length || `length` bytes
  string_list = u32 BE count  || count × string
"""

from __future__ import annotations

import string as _string
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DECRYPT_TOOL = Path(__file__).resolve().parent / "decrypt-blob"
ECIES_TOOL = Path(__file__).resolve().parent / "ecies-decrypt"
ECIES_KEY = Path(__file__).resolve().parent.parent.parent / "captures" / "CK_PV.pkcs8.der"
B64URL_CHARS = set((_string.ascii_letters + _string.digits + "-_=").encode())


# Field-index labels for the 17-field metadata payload, derived by
# inspecting the U4025QW + U3224KB samples and cross-referencing libhub.
# Indices are positional and stable across components (a slot may be
# meaningless for a given component, in which case the value is "0").
FIELD_LABELS = [
    "product",            # 0   e.g. "U4025QW"
    "version",            # 1   e.g. "1.04" or "M3T105" (per-component)
    "build_date",         # 2   "YYYY-MM-DD"
    "crc16",              # 3   4-char hex; 0000 for the second DISPLAY
    "chip_guid",          # 4   primary chip-type GUID (libhub-internal)
    "chip_guid_alt",      # 5   secondary / affiliated chip-type GUID
    "usb_vid",            # 6   "0x0bda" etc.
    "usb_pid",            # 7   "0x1100" etc.
    "i2c_addr_or_idx",    # 8   "0xD4", "0x21", "1", or "0"
    "param9",
    "param10",
    "param11",
    "param12",
    "param13",
    "param14",
    "flash_off_or_size",  # 15  "0x800", "0x109B"
    "flash_size_or_end",  # 16  "0x4400", "0x223B"
]


# ---------- low-level reader ----------------------------------------------


class Reader:
    __slots__ = ("data", "off")
    def __init__(self, data: bytes, off: int = 0) -> None:
        self.data = data
        self.off = off

    def remaining(self) -> int:
        return len(self.data) - self.off

    def u32(self) -> int:
        if self.off + 4 > len(self.data):
            raise ValueError(f"u32 past EOF @{self.off:#x}")
        v = int.from_bytes(self.data[self.off : self.off + 4], "big")
        self.off += 4
        return v

    def bytes(self, n: int) -> bytes:
        if self.off + n > len(self.data):
            raise ValueError(f"read {n} bytes past EOF @{self.off:#x}")
        b = self.data[self.off : self.off + n]
        self.off += n
        return b

    def string(self) -> bytes:
        n = self.u32()
        return self.bytes(n)

    def string_list(self) -> list[bytes]:
        n = self.u32()
        return [self.string() for _ in range(n)]


# ---------- decrypt helper -------------------------------------------------


def decrypt_metadata_field(passphrase: str, b: bytes) -> str | None:
    """Decrypt a Wistron@<MODEL>-encrypted Base64URL metadata string."""
    if not b or any(c not in B64URL_CHARS for c in b):
        return None
    try:
        out = subprocess.run(
            [str(DECRYPT_TOOL), passphrase, b.decode("ascii")],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if out.returncode != 0:
            return None
        return out.stdout.rstrip("\n")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ---------- model ----------------------------------------------------------


@dataclass
class Component:
    key_raw: bytes               # the on-disk first string
    key: str                     # printable form: plaintext name or decrypted panel_id
    panel_bound: bool            # True if `key` was an encrypted panel_id
    fields_raw: list[bytes]      # 17 Base64URL-encrypted strings
    fields: list[str | None]     # decrypted fields (None if not b64 / decrypt failed)
    payload: bytes | None = None # decrypted firmware (binary section)


@dataclass
class Upg:
    path: Path
    raw: bytes
    magic: str
    format_version: str
    product: str
    fw_version: str
    name_table: list[str]
    panel_bound: list[str]
    components: list[Component] = field(default_factory=list)
    binary_section_at: int = 0
    trailer: bytes = b""

    @property
    def passphrase(self) -> str:
        return f"Wistron@{self.product}"


# ---------- parser ---------------------------------------------------------


def parse_upg(data: bytes) -> Upg:
    r = Reader(data)
    magic = r.string().decode("ascii")
    fmt   = r.string().decode("ascii")
    prod  = r.string().decode("ascii")
    fwver = r.string().decode("ascii")

    name_table  = [s.decode("ascii") for s in r.string_list()]
    panel_bound = [s.decode("ascii") for s in r.string_list()]

    upg = Upg(path=Path(""), raw=data, magic=magic, format_version=fmt,
              product=prod, fw_version=fwver,
              name_table=name_table, panel_bound=panel_bound)

    # Components: u32 N + N × (key + 17 fields).
    n = r.u32()
    plaintext_names = set(name_table)
    for _ in range(n):
        key_raw = r.string()
        try:
            key_ascii = key_raw.decode("ascii")
        except UnicodeDecodeError:
            key_ascii = ""
        if key_ascii in plaintext_names:
            key = key_ascii
            panel_bound_flag = False
        else:
            decrypted = decrypt_metadata_field(upg.passphrase, key_raw)
            key = decrypted if decrypted is not None else f"<unparsed: {key_raw[:32]!r}>"
            panel_bound_flag = decrypted is not None

        fields_raw = [r.string() for _ in range(17)]
        fields = [decrypt_metadata_field(upg.passphrase, f) for f in fields_raw]
        upg.components.append(Component(
            key_raw=key_raw, key=key, panel_bound=panel_bound_flag,
            fields_raw=fields_raw, fields=fields,
        ))

    # Binary section.
    upg.binary_section_at = r.off
    if r.remaining() < 4:
        return upg
    m = r.u32()
    expected_keys = {c.key_raw for c in upg.components}
    for _ in range(m):
        if r.remaining() < 4:
            break
        key_raw = r.string()
        if r.remaining() < 4:
            break
        payload = r.string()  # length-prefixed binary blob
        # Match this entry to a component by key_raw.
        for c in upg.components:
            if c.payload is None and c.key_raw == key_raw:
                c.payload = payload
                break

    upg.trailer = data[r.off :]
    return upg


# ---------- pretty-printer -------------------------------------------------


def report(upg: Upg) -> None:
    print(f"# {upg.path or '(stdin)'}: {len(upg.raw):,} bytes")
    print(f"# magic={upg.magic!r}  fmt={upg.format_version!r}  "
          f"product={upg.product!r}  fw_version={upg.fw_version!r}")
    print(f"# passphrase={upg.passphrase!r}")
    print(f"# name_table:  {upg.name_table}")
    print(f"# panel_bound: {upg.panel_bound}")
    print(f"# components:  {len(upg.components)}  "
          f"(expect {len(upg.name_table) + len(upg.panel_bound)})")
    print()
    for i, c in enumerate(upg.components):
        marker = "(panel-bound)" if c.panel_bound else ""
        print(f"--- component[{i}] key={c.key!r} {marker}")
        for label, raw, val in zip(FIELD_LABELS, c.fields_raw, c.fields):
            print(f"      {label:18s} = {val!r}" if val is not None
                  else f"      {label:18s} = <not b64> {raw[:32]!r}")
        if c.payload:
            print(f"      payload          : {len(c.payload):,} bytes  "
                  f"head={c.payload[:16].hex()}…")
        else:
            print(f"      payload          : (none)")
        print()
    if upg.trailer:
        print(f"--- trailer: {len(upg.trailer)} bytes @ {upg.binary_section_at:#06x}+...")
        print(f"    head: {upg.trailer[:64].hex()}")


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path/to/file.upg>", file=sys.stderr)
        sys.exit(2)
    path = Path(sys.argv[1])
    upg = parse_upg(path.read_bytes())
    upg.path = path
    report(upg)


if __name__ == "__main__":
    main()
