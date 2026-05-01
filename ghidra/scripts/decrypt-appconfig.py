#!/usr/bin/env python3
"""
Probe tool: try to decrypt appconfig.dat from the Dell U4025QW firmware
update package.

What we know:
  * Main `Firmware Updater` binary references CryptoPP types: SHA256,
    HMAC-SHA256, HKDF (`HKDF(`), CBC mode (CipherModeFinalTemplate with
    CBC_Encryption/Decryption), AESNI. Plus a literal "passphrase"
    string and a literal "Salt" string.
  * libdevices.so / libhub.so use the same primitives in their
    `decrypt_string()` helper.
  * cert2.dat = b"\xde\xad\xbe\xef" + 22-char Base64URL → decodes to the
    16-byte block `a381eba3c5fa0c49ac48eec32fcc2e25`. Strongest candidate
    for the master key / passphrase / IKM.
  * cert.dat (18 bytes) is the synkey seed; tried for completeness.

Strategy: enumerate (header_skip, trailer_skip, key_derivation, mode, iv_source)
combinations and score the output by:
  - "{" appearing early (looks like JSON)
  - chip-type / slot-name strings appearing
  - high printable-ASCII fraction
"""

from __future__ import annotations

import base64
import itertools
from dataclasses import dataclass
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Hash import HMAC, SHA256
from Crypto.Protocol.KDF import HKDF, PBKDF2

EXTRACTED = Path(
    "/agents/ada/projects/dell-u4025qw-fw/extracted/usr/share/Dell/firmware/U4025QW"
)


def load(name: str) -> bytes:
    return (EXTRACTED / name).read_bytes()


CERT_DAT = load("cert.dat")
CERT2_DAT = load("cert2.dat")
APPCONFIG = load("appconfig.dat")

CERT2_KEY = base64.urlsafe_b64decode(
    CERT2_DAT[4:] + b"=" * ((4 - len(CERT2_DAT[4:]) % 4) % 4)
)
assert len(CERT2_KEY) == 16

# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

MARKERS = (
    b"PARADE", b"FL5500", b"REALTEK", b"RTS5409", b"MICROCHIP", b"PS5512",
    b"HUB", b"PDC", b"DISPLAY", b"AUDIO", b"TOUCH", b"BRIDGE", b"WEBCAM",
    b"TBT", b"MCU", b"db129cb6", b"55afe793", b"libhub", b"libpdc",
    b"libdisplay", b"slot", b"chip", b"name",
)


@dataclass
class Hit:
    label: str
    plaintext: bytes
    printable: float
    markers: list[bytes]
    json_brace: bool

    @property
    def score(self) -> float:
        return self.printable + 5 * len(self.markers) + (3 if self.json_brace else 0)

    def show(self, max_pt: int = 160) -> None:
        head = self.plaintext[:max_pt]
        ascii_head = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in head)
        flag = "★" if self.markers or self.json_brace else " "
        print(f"  {flag} score={self.score:.2f} pri={self.printable:.2f} "
              f"hits={self.markers} json={self.json_brace}")
        print(f"      label: {self.label}")
        print(f"      head:  {head[:64].hex()}")
        print(f"      ascii: {ascii_head}")


def score_pt(label: str, pt: bytes) -> Hit:
    if not pt:
        return Hit(label, b"", 0.0, [], False)
    pri = sum(0x20 <= b < 0x7F or b in (9, 10, 13) for b in pt) / len(pt)
    markers = [m for m in MARKERS if m in pt]
    jb = b"{" in pt[:64] or b"[" in pt[:64]
    return Hit(label, pt, pri, markers, jb)


# --------------------------------------------------------------------------
# Decryption attempts
# --------------------------------------------------------------------------


def aes_cbc(key: bytes, iv: bytes, ct: bytes) -> bytes | None:
    if len(ct) % 16 != 0:
        return None
    return AES.new(key, AES.MODE_CBC, iv).decrypt(ct)


def aes_cfb(key: bytes, iv: bytes, ct: bytes) -> bytes | None:
    return AES.new(key, AES.MODE_CFB, iv, segment_size=128).decrypt(ct)


def aes_ctr(key: bytes, nonce: bytes, ct: bytes) -> bytes | None:
    if len(nonce) > 16:
        nonce = nonce[:16]
    # CTR with explicit nonce
    return AES.new(key, AES.MODE_CTR, nonce=nonce[:8],
                   initial_value=int.from_bytes(nonce[8:16], "big") if len(nonce) >= 16 else 0
                   ).decrypt(ct)


def hkdf_split(ikm: bytes, salt: bytes, info: bytes, total: int = 64) -> bytes:
    return HKDF(master=ikm, key_len=total, salt=salt, hashmod=SHA256, context=info)


# --------------------------------------------------------------------------
# Run all combinations
# --------------------------------------------------------------------------


def main() -> None:
    print(f"appconfig.dat: {len(APPCONFIG)} bytes; cert2 key: {CERT2_KEY.hex()}")
    print()

    ikm_candidates = {
        "cert2.key": CERT2_KEY,
        "cert.dat": CERT_DAT,
        "cert2.b64ascii": CERT2_DAT[4:],
        "cert2.raw": CERT2_DAT,
        # Maybe the "passphrase" literal string is itself the passphrase:
        "lit-passphrase": b"passphrase",
        "lit-Dell": b"Dell",
        "lit-DDPM": b"DDPM",
        "lit-monitorfirmwareupdate": b"monitorfirmwareupdateutility",
        "lit-U4025QW": b"U4025QW",
    }
    info_candidates = (b"", b"passphrase", b"info", b"Salt", b"appconfig",
                       b"Dell", b"DDPM", b"key", b"AES")

    # File splits: (header bytes consumed by salt/IV, trailer bytes for MAC)
    # We try various sizes; most plausible structures:
    splits = [
        (0, 0), (0, 16), (0, 32),
        (4, 0), (4, 16), (4, 32),
        (8, 0), (8, 16), (8, 32),
        (12, 16), (12, 32),
        (16, 0), (16, 16), (16, 32),
        (24, 16), (24, 32),
        (32, 0), (32, 16), (32, 32),
    ]

    hits: list[Hit] = []

    # ---- Family 1: cert2.key used directly as AES-128 key, IV from header ----
    print("=== Family 1: direct AES-128 with cert2.key, IV=header bytes ===")
    for hdr in (0, 4, 8, 12, 16):
        iv = APPCONFIG[:hdr].ljust(16, b"\x00")[:16] if hdr > 0 else b"\x00" * 16
        for trail in (0, 16, 32):
            ct = APPCONFIG[hdr:len(APPCONFIG) - trail]
            for name, fn in (("CBC", aes_cbc), ("CFB", aes_cfb)):
                pt = fn(CERT2_KEY, iv, ct) if name != "CBC" else aes_cbc(CERT2_KEY, iv, ct)
                if pt is None:
                    continue
                h = score_pt(f"AES-128-{name} k=cert2.key iv=hdr[:{hdr}] hdr={hdr} trail={trail}",
                             pt)
                hits.append(h)

    # ---- Family 2: HKDF(IKM, salt=first N bytes, info=...) → 64 bytes:
    # 16 AES key + 16 IV + 32 MAC key. Decrypt with AES-CBC. ----
    print("=== Family 2: HKDF-SHA256 → AES-CBC ===")
    for hdr_salt_size in (0, 4, 8, 12, 16):
        for ikm_label, ikm in ikm_candidates.items():
            if not ikm:
                continue
            salt = APPCONFIG[:hdr_salt_size]
            for info in info_candidates:
                try:
                    keymat = hkdf_split(ikm, salt, info, total=64)
                    aes_key = keymat[:16]
                    iv = keymat[16:32]
                    # mac_key = keymat[32:64]  # we'll just check decryption shape
                    for trail in (0, 16, 32):
                        ct = APPCONFIG[hdr_salt_size:len(APPCONFIG) - trail]
                        if len(ct) % 16 == 0 and len(ct) > 0:
                            pt = aes_cbc(aes_key, iv, ct)
                            if pt is None:
                                continue
                            h = score_pt(
                                f"HKDF-CBC ikm={ikm_label} info={info!r} salt=hdr[:{hdr_salt_size}] trail={trail}",
                                pt,
                            )
                            hits.append(h)
                except Exception as e:
                    pass

    # ---- Family 3: HKDF derived AES key, but IV is in the file header ----
    print("=== Family 3: HKDF-SHA256(IKM only) → AES-CBC, IV from header ===")
    for ikm_label, ikm in ikm_candidates.items():
        if not ikm:
            continue
        for info in info_candidates:
            for hdr_iv_at, iv_size in [(0, 16), (4, 16), (8, 16)]:
                iv = APPCONFIG[hdr_iv_at:hdr_iv_at + iv_size]
                if len(iv) != 16:
                    continue
                ct_start = hdr_iv_at + iv_size
                for trail in (0, 16, 32):
                    ct = APPCONFIG[ct_start:len(APPCONFIG) - trail]
                    if len(ct) % 16 != 0 or len(ct) == 0:
                        continue
                    try:
                        keymat = hkdf_split(ikm, b"", info, total=32)
                        pt = aes_cbc(keymat[:16], iv, ct)
                        if pt is None:
                            continue
                        h = score_pt(
                            f"HKDF-CBC-iv-in-file ikm={ikm_label} info={info!r} iv@{hdr_iv_at} trail={trail}",
                            pt,
                        )
                        hits.append(h)
                    except Exception:
                        pass

    # ---- Family 3.5: HKDF + AES-CFB (no alignment needed) ----
    print("=== Family 3.5: HKDF-SHA256 → AES-CFB128, IV from KDF or header ===")
    for ikm_label, ikm in ikm_candidates.items():
        if not ikm:
            continue
        for info in info_candidates:
            for hdr_salt_size in (0, 4, 8, 12, 16):
                for iv_source in ("kdf", "header"):
                    for trail in (0, 16, 32):
                        try:
                            salt = APPCONFIG[:hdr_salt_size]
                            keymat = hkdf_split(ikm, salt, info, total=64)
                            aes_key = keymat[:16]
                            if iv_source == "kdf":
                                iv = keymat[16:32]
                                ct_start = hdr_salt_size
                            else:
                                iv = APPCONFIG[hdr_salt_size:hdr_salt_size + 16]
                                if len(iv) != 16:
                                    continue
                                ct_start = hdr_salt_size + 16
                            ct = APPCONFIG[ct_start:len(APPCONFIG) - trail]
                            if not ct:
                                continue
                            pt = aes_cfb(aes_key, iv, ct)
                            h = score_pt(
                                f"HKDF-CFB ikm={ikm_label} info={info!r} salt=hdr[:{hdr_salt_size}] "
                                f"iv={iv_source} trail={trail}",
                                pt,
                            )
                            hits.append(h)
                        except Exception:
                            pass

    # ---- Family 3.6: HKDF + AES-CTR (no alignment) ----
    print("=== Family 3.6: HKDF-SHA256 → AES-CTR ===")
    for ikm_label, ikm in ikm_candidates.items():
        if not ikm:
            continue
        for info in info_candidates:
            for hdr_salt_size in (0, 4, 8, 12, 16):
                for trail in (0, 16, 32):
                    try:
                        salt = APPCONFIG[:hdr_salt_size]
                        keymat = hkdf_split(ikm, salt, info, total=32)
                        aes_key = keymat[:16]
                        nonce = keymat[16:32]
                        ct = APPCONFIG[hdr_salt_size:len(APPCONFIG) - trail]
                        if not ct:
                            continue
                        pt = aes_ctr(aes_key, nonce, ct)
                        h = score_pt(
                            f"HKDF-CTR ikm={ikm_label} info={info!r} salt=hdr[:{hdr_salt_size}] trail={trail}",
                            pt,
                        )
                        hits.append(h)
                    except Exception:
                        pass

    # ---- Family 4: PBKDF2 ----
    print("=== Family 4: PBKDF2-SHA256 → AES-CBC ===")
    for ikm_label, ikm in ikm_candidates.items():
        if not ikm:
            continue
        for hdr_salt_size in (0, 8, 16):
            salt = APPCONFIG[:hdr_salt_size] or b"\x00" * 8
            for iters in (1000, 2500, 10000):
                try:
                    keymat = PBKDF2(ikm, salt, dkLen=48, count=iters, hmac_hash_module=SHA256)
                    aes_key = keymat[:16]
                    iv = keymat[16:32]
                    for trail in (0, 16, 32):
                        ct = APPCONFIG[hdr_salt_size:len(APPCONFIG) - trail]
                        if len(ct) % 16 != 0 or len(ct) == 0:
                            continue
                        pt = aes_cbc(aes_key, iv, ct)
                        if pt is None:
                            continue
                        h = score_pt(
                            f"PBKDF2-CBC ikm={ikm_label} iters={iters} salt=hdr[:{hdr_salt_size}] trail={trail}",
                            pt,
                        )
                        hits.append(h)
                except Exception:
                    pass

    # ---- Sort and show top 10 ----
    print()
    print(f"=== Top 10 of {len(hits)} attempts (by score) ===")
    hits.sort(key=lambda h: -h.score)
    for h in hits[:10]:
        h.show()
        print()


if __name__ == "__main__":
    main()
