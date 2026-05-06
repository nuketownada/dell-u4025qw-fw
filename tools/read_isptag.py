#!/usr/bin/env python3
"""Read the U4025QW IspTag register from the live monitor.

Replicates the read sequence from the dell-monitor-rt fwupd plugin's
setup() phase: enable_vdcmd -> enable_high_clock -> cal_auth handshake
-> DDC/CI VCP 0xAD read on slave 0x6e. Pure read — no chip-side flash
writes.

Outputs the prefix (ISP/CHK/UNKNOWN) and version field.
"""
import fcntl
import os
import sys
import time

DEV = "/dev/hidraw2"
REPORT_SIZE = 192
BUF_SIZE = 1 + REPORT_SIZE  # +1 for HID report-ID prefix (= 0)

# U4025QW synkey seed (cert.dat).  See plugin
# fu-dell-monitor-rt-device.c DELL_MONITOR_RT_U4025QW_SYNKEY_SEED.
SYNKEY_SEED = bytes(
    [
        0xF8, 0xB7, 0xFD, 0x21, 0xE0, 0x32, 0x22, 0xB8,
        0xA9, 0xE8, 0x7C, 0x11, 0x04, 0x94, 0xE2, 0x9D,
        0x9F, 0x6A,
    ]
)


def derive_synkey(seed):
    """Port of RTS5409S_HID::get_synkey() — produces 8-byte cal_auth key."""
    key = bytearray(8)
    in_idx = 0
    out_idx = 0
    while in_idx < len(seed) and out_idx < 8:
        trigger = seed[in_idx]
        bits = trigger & 0x11
        if bits in (0x01, 0x10):  # FAST: 2 in -> 1 out
            if in_idx + 1 >= len(seed):
                break
            key[out_idx] = trigger ^ seed[in_idx + 1]
            in_idx += 2
            out_idx += 1
        elif bits == 0x11:  # SLOW: 3 in -> 2 out
            if in_idx + 2 >= len(seed):
                break
            if out_idx < 8:
                key[out_idx] = trigger ^ seed[in_idx + 1]
            if out_idx + 1 < 8:
                key[out_idx + 1] = seed[in_idx + 2] ^ seed[in_idx + 1]
            in_idx += 3
            out_idx += 2
        else:  # SKIP
            in_idx += 1
    return bytes(key)


def cal_auth(challenge, key):
    """Port of RTS5409S_HID::cal_auth — 8-byte response from 16-byte challenge."""
    mix = (challenge[0] << 8) | challenge[15]
    parity_even = bin(mix).count("1") % 2 == 0
    if parity_even:
        tmp = bytearray(challenge[8:16])
        idx = challenge[6] & 0x07
        tmp[idx] ^= challenge[idx]
    else:
        tmp = bytearray(challenge[0:8])
        idx = challenge[14] & 0x07
        tmp[idx] ^= challenge[idx + 8]
    return bytes(tmp[i] ^ key[i] for i in range(8))


def HIDIOCGINPUT(length):
    """ioctl number for hidraw GET_REPORT (input).

    Kernel header: #define HIDIOCGINPUT(len) _IOC(_IOC_READ|_IOC_WRITE, 'H', 0x0A, len)
    so dir = 3 (READ|WRITE), not just READ.
    """
    return (3 << 30) | (length << 16) | (ord("H") << 8) | 0x0A


def make_buf():
    return bytearray(BUF_SIZE)


def vcmd(fd, dir_b, opcode, sub, arg, payload=b""):
    buf = make_buf()
    buf[1] = dir_b
    buf[2] = opcode
    buf[3] = sub
    buf[4] = arg
    if payload:
        buf[5 : 5 + len(payload)] = payload
    n = os.write(fd, bytes(buf))
    if n != BUF_SIZE:
        raise RuntimeError(f"short write: {n}/{BUF_SIZE}")


def vcmd_read(fd, dir_b, opcode, sub, arg, payload=b""):
    vcmd(fd, dir_b, opcode, sub, arg, payload)
    out = bytearray(BUF_SIZE)
    fcntl.ioctl(fd, HIDIOCGINPUT(BUF_SIZE), out, True)
    return bytes(out)


def i2c_write_via_tunnel(fd, slave, payload):
    buf = make_buf()
    buf[1] = 0x40  # DIR_WRITE
    buf[2] = 0xC6  # OPCODE_I2C_WRITE
    buf[1 + 6] = len(payload)  # WIRE_LEN_OFFSET
    buf[1 + 8] = slave  # WIRE_TARGET_OFFSET
    buf[1 + 10] = 0x00  # WIRE_SPEED_OFFSET (default)
    buf[1 + 64 : 1 + 64 + len(payload)] = payload  # WIRE_DATA_OFFSET
    n = os.write(fd, bytes(buf))
    if n != BUF_SIZE:
        raise RuntimeError(f"short write: {n}/{BUF_SIZE}")


def i2c_read_via_tunnel(fd, slave, count):
    buf = make_buf()
    buf[1] = 0x40  # DIR_WRITE (yes — read req frame is dir=write)
    buf[2] = 0xD6  # OPCODE_I2C_READ
    buf[1 + 6] = count
    buf[1 + 8] = slave
    buf[1 + 10] = 0x00
    n = os.write(fd, bytes(buf))
    if n != BUF_SIZE:
        raise RuntimeError(f"short write: {n}/{BUF_SIZE}")
    out = bytearray(BUF_SIZE)
    fcntl.ioctl(fd, HIDIOCGINPUT(BUF_SIZE), out, True)
    return bytes(out)


def main():
    if not os.path.exists(DEV):
        sys.exit(f"{DEV} not present")
    print(f"opening {DEV}")
    fd = os.open(DEV, os.O_RDWR)
    try:
        # 1. enable_vdcmd with the RealTek vendor signature 0x0BDA (LE)
        vcmd(fd, 0x40, 0x02, 0x01, 0x00, b"\xDA\x0B")
        # 2. enable_high_clock
        vcmd(fd, 0x40, 0x06, 0x01, 0x00)
        # 3. cal_auth: pull 16-byte challenge
        resp = vcmd_read(fd, 0x40, 0xE1, 0x01, 0x01)
        challenge = resp[1:17]
        print(f"challenge: {challenge.hex(' ')}")
        key = derive_synkey(SYNKEY_SEED)
        print(f"derived synkey: {key.hex(' ')}")
        auth_resp = cal_auth(challenge, key)
        print(f"auth response: {auth_resp.hex(' ')}")
        # 4. cal_auth: send 8-byte response at wire offset 64
        buf = make_buf()
        buf[1] = 0x40
        buf[2] = 0xE1
        buf[3] = 0x03
        buf[4] = 0x00
        buf[1 + 64 : 1 + 64 + 8] = auth_resp
        n = os.write(fd, bytes(buf))
        if n != BUF_SIZE:
            raise RuntimeError(f"short write: {n}/{BUF_SIZE}")
        # 5. i2c write of the IspTag VCP read-setup (51 84 c0 99 ad 18 57)
        i2c_write_via_tunnel(
            fd, 0x6E, bytes([0x51, 0x84, 0xC0, 0x99, 0xAD, 0x18, 0x57])
        )
        time.sleep(0.05)
        # 6. i2c read 0x40 bytes back
        out = i2c_read_via_tunnel(fd, 0x6E, 0x40)
        wire = out[1:48]
        ascii_view = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in wire)
        print(f"\nIspTag wire: {wire.hex(' ')}")
        print(f"IspTag ascii: {ascii_view}")

        # parse
        if wire[0] != 0x51 or (wire[1] & 0x80) == 0:
            print("\nMALFORMED reply (no 0x51/length-msb)")
            return 2
        length = wire[1] & 0x7F
        body = wire[4 : 4 + length]
        text = body.decode("ascii", errors="replace")
        print(f"\nparsed body: {text!r}")
        for prefix in ("ISP#", "CHK#"):
            if prefix in text:
                start = text.index(prefix) + 4
                end = text.index("#", start) if "#" in text[start:] else len(text)
                version = text[start:end]
                print(f"\n*** RESULT: prefix={prefix.rstrip('#')} version={version!r} ***")
                return 0
        print(f"\n*** RESULT: UNKNOWN prefix; body={text!r} ***")
        return 1
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
