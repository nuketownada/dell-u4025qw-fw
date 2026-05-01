#!/usr/bin/env python3
"""
Walk a Dell .upg firmware bundle byte-by-byte and dump every structural
field it contains. Treats the file as a sequence of length-prefixed
records (some encrypted, some binary), grouped per component. Use this
to lock down the format before porting to a FuFirmware subclass.

Format hypothesis (based on initial decoding work):

  Header:
    u32 BE len + bytes   "UPG"
    u32 BE len + bytes   format version, e.g. "1.0.6"
    u32 BE len + bytes   product short name, e.g. "U4025QW"
    u32 BE len + bytes   firmware bundle version, e.g. "M3T105"
    u32 BE                component count

  Component name table:
    for each component:
      u32 BE len + bytes  component name (e.g. "HUB1")

  Per-component sections (one per component, but order may differ from
  the name table):
    ... TBD (this is what we're walking to figure out)

Each Base64URL string we find is a candidate metadata field; we
try to decrypt it with `Wistron@<MODEL>` (where <MODEL> comes from the
header). Each non-printable run is a candidate firmware payload.

Usage:
  walk-upg.py <path/to/file.upg>
"""

from __future__ import annotations

import string
import subprocess
import sys
from pathlib import Path

DECRYPT_TOOL = Path(__file__).resolve().parent / "decrypt-blob"
B64URL_CHARS = set(string.ascii_letters.encode() +
                   string.digits.encode() + b"-_")


def read_be32(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off : off + 4], "big")


def read_lp_string(buf: bytes, off: int) -> tuple[str, int]:
    n = read_be32(buf, off)
    return buf[off + 4 : off + 4 + n].decode("ascii", errors="replace"), off + 4 + n


def is_b64url_run(buf: bytes, off: int, length: int) -> bool:
    return all(buf[off + i] in B64URL_CHARS for i in range(length))


def decrypt(passphrase: str, b64: str) -> str | None:
    try:
        out = subprocess.run(
            [str(DECRYPT_TOOL), passphrase, b64],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if out.returncode != 0:
            return None
        s = out.stdout.rstrip("\n")
        # Sanity-check: decrypted content should be mostly printable.
        if all(0x20 <= b < 0x7F or b in (9, 10, 13) for b in s.encode()):
            return s
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def hexdump_short(buf: bytes, max_bytes: int = 32) -> str:
    return buf[:max_bytes].hex() + ("…" if len(buf) > max_bytes else "")


def walk(path: Path) -> None:
    data = path.read_bytes()
    print(f"# file: {path}")
    print(f"# size: {len(data)} bytes")
    print()

    off = 0
    print(f"--- header @ {off:#06x} ---")
    magic, off = read_lp_string(data, off)
    ver, off   = read_lp_string(data, off)
    prod, off  = read_lp_string(data, off)
    fwver, off = read_lp_string(data, off)
    comp_count = read_be32(data, off); off += 4
    print(f"  magic        = {magic!r}")
    print(f"  format ver   = {ver!r}")
    print(f"  product      = {prod!r}")
    print(f"  fw bundle    = {fwver!r}")
    print(f"  comp count   = {comp_count}")

    passphrase = f"Wistron@{prod}"
    print(f"  → passphrase = {passphrase!r}")
    print()

    print(f"--- component name table @ {off:#06x} ---")
    components = []
    for i in range(comp_count):
        name, off = read_lp_string(data, off)
        print(f"  [{i}] {name!r}")
        components.append(name)
    print()

    body_start = off
    print(f"--- body starts @ {off:#06x} (file_size - body_start = {len(data) - off} bytes of components) ---")
    print()

    # Try parsing the body as a sequence of length-prefixed records.
    # We expect every record to be u32 BE length + length bytes. Some
    # are Base64URL strings (decrypt with Wistron@<MODEL>), some are
    # binary (firmware payloads). Print everything we see.
    record = 0
    while off < len(data) - 4:
        try:
            n = read_be32(data, off)
        except Exception:
            print(f"  @{off:#06x}: bad length header, stopping")
            break

        # Sanity: 0 <= n <= remaining bytes of file.
        if n == 0 or off + 4 + n > len(data):
            print(f"  @{off:#06x}: u32={n} doesn't fit (rem={len(data) - off - 4}); stopping")
            break

        payload_off = off + 4
        payload = data[payload_off : payload_off + n]
        next_off = payload_off + n

        # Classify.
        if is_b64url_run(data, payload_off, min(n, 256)) and n >= 8:
            # Could be a Base64URL ciphertext.
            pt = decrypt(passphrase, payload.decode("ascii"))
            if pt is not None:
                kind = "ENC "
                desc = f"{pt!r}"
            else:
                kind = "B64?"
                desc = f"{payload[:64].decode('ascii', errors='replace')!r}…"
        elif n <= 32 and all(0x20 <= b < 0x7F for b in payload):
            kind = "TXT "
            desc = f"{payload.decode('ascii', errors='replace')!r}"
        else:
            kind = "BIN "
            desc = f"len={n} sha?={hash(payload) & 0xffff_ffff:#010x} head={hexdump_short(payload)}"

        print(f"  rec[{record:3d}] @{off:#06x}  u32_len={n:6d}  {kind} {desc}")
        record += 1
        off = next_off

    print()
    print(f"--- end of recognized records, off={off:#06x}, file_size={len(data)} ---")
    if off < len(data):
        print(f"  trailing {len(data) - off} bytes: {hexdump_short(data[off:], 64)}")


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path/to/file.upg>", file=sys.stderr)
        sys.exit(2)
    walk(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
