#!/usr/bin/env python3
"""
Walk a Dell .upg firmware bundle and decrypt every metadata blob using the
per-product passphrase scheme `Wistron@<MODEL>` (the model is parsed out
of the .upg header).

Usage:
  dump-upg-metadata.py <path/to/file.upg>

Requires the `decrypt-blob` tool sibling, built from decrypt-blob.cpp
(see scripts/README.md).
"""

from __future__ import annotations

import re
import string
import subprocess
import sys
from pathlib import Path

DECRYPT_TOOL = Path(__file__).resolve().parent / "decrypt-blob"

PRINTABLE = set(string.printable.encode())
B64URL_CHARS = set(string.ascii_letters.encode() + string.digits.encode() +
                   b"-_")  # Base64URL, no padding (CryptoPP doesn't add `=`)


def read_be32(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off : off + 4], "big")


def read_lp_string(buf: bytes, off: int) -> tuple[str, int]:
    n = read_be32(buf, off)
    return buf[off + 4 : off + 4 + n].decode("ascii"), off + 4 + n


def find_b64url_runs(buf: bytes, min_len: int = 80) -> list[tuple[int, bytes]]:
    """Find every long contiguous run of Base64URL characters in buf."""
    runs = []
    i = 0
    n = len(buf)
    while i < n:
        if buf[i] in B64URL_CHARS:
            j = i + 1
            while j < n and buf[j] in B64URL_CHARS:
                j += 1
            if j - i >= min_len:
                runs.append((i, buf[i:j]))
            i = j
        else:
            i += 1
    return runs


def decrypt(passphrase: str, b64: str) -> str | None:
    """Run our C++ decrypter on one blob; return plaintext or None."""
    try:
        out = subprocess.run(
            [str(DECRYPT_TOOL), passphrase, b64],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if out.returncode != 0:
            return None
        return out.stdout.rstrip("\n")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path/to/file.upg>", file=sys.stderr)
        sys.exit(2)
    path = Path(sys.argv[1])
    data = path.read_bytes()
    print(f"# {path}: {len(data)} bytes")

    # Header walk: <magic="UPG"> <ver> <product> <fw_ver> <component_count>
    # All length-prefixed strings are u32 BE length + ASCII bytes.
    off = 0
    magic, off = read_lp_string(data, off)
    ver, off   = read_lp_string(data, off)
    prod, off  = read_lp_string(data, off)
    fwver, off = read_lp_string(data, off)
    comp_count = read_be32(data, off); off += 4

    print(f"# magic     = {magic!r}")
    print(f"# version   = {ver!r}")
    print(f"# product   = {prod!r}")
    print(f"# fw ver    = {fwver!r}")
    print(f"# comp cnt  = {comp_count}")

    passphrase = f"Wistron@{prod}"
    print(f"# passphrase= {passphrase!r}")

    # Walk component-name table.
    components = []
    for _ in range(comp_count):
        name, off = read_lp_string(data, off)
        components.append(name)
    print(f"# components= {components}")
    print()

    # Find every Base64URL run in the file and try to decrypt it.
    runs = find_b64url_runs(data, min_len=88)
    print(f"# scanning {len(runs)} candidate B64URL runs >= 88 chars\n")
    for off, raw in runs:
        s = raw.decode("ascii")
        pt = decrypt(passphrase, s)
        if pt is None or not all(0x20 <= b < 0x7F or b in (9, 10, 13)
                                  for b in pt.encode()[:60]):
            continue  # decrypt failed or returned binary garbage
        print(f"@0x{off:06x}  ({len(s):3d} chars)  →  {pt!r}")


if __name__ == "__main__":
    main()
