#!/usr/bin/env python3
"""
Structured .upg parser.

The .upg format (reverse-engineered for the Dell U4025QW M3T105 bundle):

  [HEADER]
    u32 BE len + bytes   "UPG"
    u32 BE len + bytes   format version, e.g. "1.0.6"
    u32 BE len + bytes   product short name, e.g. "U4025QW"
    u32 BE len + bytes   firmware bundle version, e.g. "M3T105"
    u32 BE                component_count

  [COMPONENT NAME TABLE]
    for _ in range(component_count):
      u32 BE len + bytes  component name (e.g. "HUB1")

  [BODY]
    Sequence of length-prefixed records (u32 BE length + bytes). The
    body groups records into per-component sections. Each section
    starts with a marker triple:
        u32 BE = 1                            (schema/version marker)
        u32 BE len + bytes                    (re-emitted component name)
        u32 BE = N                            (count of metadata records to follow)
    followed by N records (each u32 BE + bytes, encrypted Base64URL).
    After the metadata records, the binary firmware payload follows
    as one large length-prefixed BIN record.
    Sections appear in some order that may differ from the name table.
"""

from __future__ import annotations

import string
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

DECRYPT_TOOL = Path(__file__).resolve().parent / "decrypt-blob"
B64URL_CHARS = set(string.ascii_letters.encode() +
                   string.digits.encode() + b"-_")


def be32(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off : off + 4], "big")


def lp_string(buf: bytes, off: int) -> tuple[str, int]:
    n = be32(buf, off)
    return buf[off + 4 : off + 4 + n].decode("ascii", errors="replace"), off + 4 + n


def lp_bytes(buf: bytes, off: int) -> tuple[bytes, int]:
    n = be32(buf, off)
    return buf[off + 4 : off + 4 + n], off + 4 + n


def is_b64url(b: bytes) -> bool:
    return len(b) >= 8 and all(c in B64URL_CHARS for c in b)


def decrypt(passphrase: str, b64: str) -> str | None:
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


@dataclass
class UpgRecord:
    offset: int
    raw: bytes
    decrypted: str | None = None  # populated for B64URL records

    @property
    def is_metadata(self) -> bool:
        return is_b64url(self.raw)

    @property
    def is_binary(self) -> bool:
        return not self.is_metadata and len(self.raw) > 64


@dataclass
class UpgComponent:
    name: str
    name_at: int
    record_count: int
    metadata: list[UpgRecord] = field(default_factory=list)
    payload: UpgRecord | None = None  # the big binary blob, if found in this section


@dataclass
class UpgFile:
    path: Path
    raw: bytes
    magic: str
    version: str
    product: str
    fw_version: str
    component_names: list[str]
    components: list[UpgComponent]
    trailing: bytes  # whatever's left after the structured parse

    @property
    def passphrase(self) -> str:
        return f"Wistron@{self.product}"


def parse_header(data: bytes) -> tuple[UpgFile, int]:
    """Parse header + component-name table. Returns (UpgFile, body_offset)."""
    off = 0
    magic, off = lp_string(data, off)
    version, off = lp_string(data, off)
    product, off = lp_string(data, off)
    fw_version, off = lp_string(data, off)
    comp_count = be32(data, off); off += 4

    names = []
    for _ in range(comp_count):
        name, off = lp_string(data, off)
        names.append(name)

    upg = UpgFile(
        path=Path(""), raw=data, magic=magic, version=version, product=product,
        fw_version=fw_version, component_names=names, components=[], trailing=b"",
    )
    return upg, off


def parse_lp_records(data: bytes, off: int) -> Iterator[UpgRecord]:
    """Yield length-prefixed records (u32 BE length + bytes) starting at off."""
    while off + 4 <= len(data):
        rlen = be32(data, off)
        if rlen == 0 or off + 4 + rlen > len(data):
            return
        rec = UpgRecord(offset=off, raw=data[off + 4 : off + 4 + rlen])
        yield rec
        off = off + 4 + rlen


def parse(data: bytes) -> UpgFile:
    upg, off = parse_header(data)
    expected = set(upg.component_names)

    # Body schema:
    #   1) bare u32 BE = 1   (start-of-body marker, present once at file start)
    #   2) a sequence of length-prefixed records (u32 BE + bytes). A record
    #      whose payload matches a component-table name marks the start of
    #      that component's metadata block.
    #   3) right after the FIRST name re-emission there is a bare u32 BE
    #      "field count" (e.g. 7) that's NOT a length-prefixed record.
    #      Subsequent components' name re-emissions are not followed by
    #      this extra u32.
    #   4) all other records are encrypted metadata blobs, with the
    #      occasional binary-looking records that probably wrap firmware
    #      payloads (we'll classify those by content).
    if off + 4 > len(data):
        upg.trailing = data[off:]
        return upg
    marker = be32(data, off)
    if marker != 1:
        upg.trailing = data[off:]
        return upg
    off += 4

    current: UpgComponent | None = None
    seen_first_name = False
    while off + 4 <= len(data):
        rlen = be32(data, off)
        if rlen == 0 or off + 4 + rlen > len(data):
            break
        payload = data[off + 4 : off + 4 + rlen]
        rec = UpgRecord(offset=off, raw=payload)

        # Test for name re-emission.
        try:
            as_str = payload.decode("ascii") if 1 <= rlen <= 64 else ""
        except UnicodeDecodeError:
            as_str = ""

        if as_str in expected:
            current = UpgComponent(name=as_str, name_at=off, record_count=0)
            upg.components.append(current)
            off += 4 + rlen
            # After the FIRST component's name, skip a bare u32 ("field count").
            if not seen_first_name:
                seen_first_name = True
                if off + 4 <= len(data):
                    off += 4   # skip the bare u32
            continue

        if current is None:
            current = UpgComponent(name="_pre", name_at=-1, record_count=0)
            upg.components.append(current)
        current.metadata.append(rec)
        current.record_count += 1
        off += 4 + rlen

    upg.trailing = data[off:]
    return upg


def decrypt_metadata(upg: UpgFile, max_attempts: int | None = None) -> None:
    """Decrypt every Base64URL metadata record in-place."""
    n = 0
    for comp in upg.components:
        for rec in comp.metadata:
            if not rec.is_metadata:
                continue
            n += 1
            if max_attempts and n > max_attempts:
                return
            pt = decrypt(upg.passphrase, rec.raw.decode("ascii"))
            rec.decrypted = pt


def report(upg: UpgFile) -> None:
    print(f"# {upg.path or '(stdin)'}: {len(upg.raw)} bytes")
    print(f"# magic={upg.magic!r}  ver={upg.version!r}  product={upg.product!r}")
    print(f"# fw_version={upg.fw_version!r}  passphrase={upg.passphrase!r}")
    print(f"# components in name table: {upg.component_names}")
    print()

    for i, comp in enumerate(upg.components):
        print(f"--- component[{i}] '{comp.name}' (re-emitted name @ {comp.name_at:#06x}) ---")
        print(f"    record count: {comp.record_count}")
        bin_records = [r for r in comp.metadata if not r.is_metadata]
        meta_records = [r for r in comp.metadata if r.is_metadata]
        print(f"    metadata records: {len(meta_records)}")
        print(f"    binary records:   {len(bin_records)}")
        for r in meta_records:
            tag = repr(r.decrypted) if r.decrypted is not None else f"<not decrypted, {len(r.raw)} chars b64>"
            print(f"      meta @{r.offset:#06x}  len={len(r.raw):3d}  {tag}")
        for r in bin_records:
            print(f"      bin  @{r.offset:#06x}  len={len(r.raw):8d}  head={r.raw[:24].hex()}…")
        print()

    print(f"--- trailing data: {len(upg.trailing)} bytes @ {len(upg.raw) - len(upg.trailing):#06x} ---")
    if upg.trailing:
        print(f"    head: {upg.trailing[:64].hex()}")


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path/to/file.upg>", file=sys.stderr)
        sys.exit(2)
    path = Path(sys.argv[1])
    upg = parse(path.read_bytes())
    upg.path = path
    decrypt_metadata(upg)
    report(upg)


if __name__ == "__main__":
    main()
