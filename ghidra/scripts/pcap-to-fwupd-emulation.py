#!/usr/bin/env python3
"""
pcap-to-fwupd-emulation.py

Convert a Linux usbmon pcap of a Dell monitor firmware update into the
fwupd device emulation file format (a ZIP archive containing per-phase
JSON snapshots of backend state) so the fwupd dell-monitor-rt plugin
can be exercised without a real device attached.

STATUS — first cut, NOT yet replay-ready. Currently emits events in the
`ControlTransfer:Direction=…,RequestType=…,Recipient=…,Request=…,Value=…,
Idx=…,Data=…,Length=…` format produced by FuUsbDevice. That matches what
a libusb-based FuUsbDevice or FuHidDevice plugin would lookup at replay
time, but our plugin (FuDellMonitorRtDevice) is a FuHidrawDevice
subclass — it goes through /dev/hidraw via fu_udev_device_write/read,
which produces `Write:Data=…,Length=…` and `Read:Length=…` events
instead. To make this file actually replay, the per-event emission below
needs to be swapped to those formats. The pcap data needed is identical
(the 192-byte payload, no setup header — the kernel constructs the
setup packet); only the event_id string and the placement of the response
data change. Tracked as a follow-up.

Specifically targets the U4025QW HID interfaces at VID 0x0bda PID
0x1100 / 0x1101 plus the bootloader-mode device(s) the monitor
re-enumerates as during the update.

The fwupd ControlTransfer event_id format is fixed (see
libfwupdplugin/fu-usb-device.c::fu_usb_device_control_transfer):

  ControlTransfer:
    Direction=0x{direction_HtoD_or_DtoH}   # bmRequestType bit 7 (0=OUT, 0x80=IN)
    RequestType=0x{0=STANDARD,1=CLASS,2=VENDOR}
    Recipient=0x{0=DEVICE,1=INTERFACE,2=ENDPOINT,3=OTHER}
    Request=0x{bRequest}
    Value=0x{wValue}
    Idx=0x{wIndex}
    Data={base64 of payload, OR empty for IN reqs}
    Length=0x{wLength}

Each event also has a top-level "Data" field containing the response
payload (base64) — the bytes returned by the device for IN requests, or
empty for OUT requests. fwupd matches incoming calls against the event
Id; on hit, replays the recorded "Data" as the device's response.

Usage:
  pcap-to-fwupd-emulation.py <input.pcapng> <output.zip>

Optional knobs (--vid/--pid for monitors other than U4025QW; --vid/--pid
can be passed multiple times to keep multiple devices in the recording):
  --vid VID --pid PID   only keep transfers to/from devices with the
                        given USB vendor/product ID (default: 0bda:1100,
                        0bda:1101, plus any bootloader-mode device whose
                        first observed control transfer matches a
                        Wistron/Realtek HID class)
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# Linux usbmon binary header (64 bytes) — see Documentation/usb/usbmon.txt.
# We parse the pcap manually rather than going through tshark because tshark's
# column extractor doesn't expose the wValue/wIndex/wLength fields cleanly for
# HID-decoded frames (they only show up under the `usbhid.*` namespace in
# verbose / JSON output, which is heavyweight). The binary header is well-
# documented and easy to parse.
# ---------------------------------------------------------------------------
import struct

# Format of the per-packet usbmon header (64 bytes total).
# c.f. Linux Documentation/usb/usbmon.txt §"Format of the binary stream"
USBMON_HDR = struct.Struct("<Q B B B B H B B q i i I I 8s i i I I")
USBMON_HDR_LEN = USBMON_HDR.size  # 64

# pcap-savefile global header.
PCAP_FILE_HDR = struct.Struct("<I H H i I I I")
PCAP_FILE_HDR_LEN = PCAP_FILE_HDR.size  # 24
# pcap per-record header.
PCAP_REC_HDR = struct.Struct("<I I I I")
PCAP_REC_HDR_LEN = PCAP_REC_HDR.size  # 16

# Linktypes.
DLT_USB_LINUX = 189
DLT_USB_LINUX_MMAPPED = 220


def iter_pcap_frames(path: Path) -> Iterator[bytes]:
    """Yield raw USB frame bytes from a (legacy) pcap file."""
    with path.open("rb") as f:
        hdr = f.read(PCAP_FILE_HDR_LEN)
        magic, vmaj, vmin, tz, sigfigs, snaplen, linktype = PCAP_FILE_HDR.unpack(hdr)
        if magic != 0xA1B2C3D4:
            sys.exit(f"not a legacy pcap file (magic {magic:#x})")
        if linktype not in (DLT_USB_LINUX, DLT_USB_LINUX_MMAPPED):
            sys.exit(f"linktype {linktype} unsupported (need DLT_USB_LINUX={DLT_USB_LINUX})")
        while True:
            rec = f.read(PCAP_REC_HDR_LEN)
            if len(rec) < PCAP_REC_HDR_LEN:
                return
            ts_sec, ts_usec, incl_len, orig_len = PCAP_REC_HDR.unpack(rec)
            data = f.read(incl_len)
            if len(data) < incl_len:
                return
            yield data


def iter_pcapng_frames(path: Path) -> Iterator[bytes]:
    """Yield raw USB frame bytes from a pcapng file. Minimal parser — just
    enough to handle wireshark-emitted captures."""
    BLOCK_HDR = struct.Struct("<I I")
    with path.open("rb") as f:
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                return
            block_type, block_len = BLOCK_HDR.unpack(hdr)
            body = f.read(block_len - 8)
            # Trailing block_len duplicate is part of body[-4:]; total len matches.
            if len(body) < block_len - 8:
                return
            if block_type == 0x0A0D0D0A:
                # Section Header Block — skip
                continue
            if block_type == 0x00000001:
                # Interface Description Block — could check linktype here
                continue
            if block_type == 0x00000006:
                # Enhanced Packet Block:
                #   u32 interface_id, u32 ts_high, u32 ts_low,
                #   u32 cap_len, u32 orig_len, packet_data, options...
                cap_len = struct.unpack("<I", body[12:16])[0]
                yield body[20 : 20 + cap_len]
                continue
            # Other blocks — skip
            continue


def iter_frames(path: Path) -> Iterator[bytes]:
    with path.open("rb") as f:
        magic = f.read(4)
    if magic == b"\xd4\xc3\xb2\xa1":
        yield from iter_pcap_frames(path)
    elif magic == b"\x0a\x0d\x0d\x0a":
        yield from iter_pcapng_frames(path)
    else:
        sys.exit(f"unrecognised file magic {magic.hex()}")


# ---------------------------------------------------------------------------
# Parse a usbmon frame into a dict.
# ---------------------------------------------------------------------------
URB_TYPE_SUBMIT, URB_TYPE_COMPLETE, URB_TYPE_ERROR = ord("S"), ord("C"), ord("E")
XFER_TYPE_CONTROL = 2
XFER_TYPE_BULK = 3


def parse_usbmon(frame: bytes) -> dict | None:
    if len(frame) < USBMON_HDR_LEN:
        return None
    fields = USBMON_HDR.unpack(frame[:USBMON_HDR_LEN])
    (urb_id, urb_type, xfer_type, epnum, devnum, busnum, flag_setup,
     flag_data, ts_sec, ts_usec, status, length, len_cap, setup_or_iso,
     interval, start_frame, xfer_flags, ndesc) = fields
    payload = frame[USBMON_HDR_LEN : USBMON_HDR_LEN + len_cap]
    return {
        "urb_id": urb_id,
        "urb_type": chr(urb_type),
        "xfer_type": xfer_type,
        "epnum": epnum,
        "devnum": devnum,
        "busnum": busnum,
        "ts": ts_sec + ts_usec / 1_000_000.0,
        "status": status,
        "length": length,
        "len_cap": len_cap,
        "setup": setup_or_iso if flag_setup == 0 else None,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# Pair Submit + Complete URBs and emit fwupd ControlTransfer events.
# ---------------------------------------------------------------------------
def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def event_id_for_control(setup: bytes, payload_out: bytes, length: int) -> str:
    """Build the fwupd ControlTransfer event_id from a setup packet + OUT
    payload. For IN transfers `payload_out` is empty."""
    bm_request_type, b_request, w_value, w_index, w_length = struct.unpack(
        "<B B H H H", setup
    )
    direction = bm_request_type & 0x80
    request_type = (bm_request_type >> 5) & 0x03
    recipient = bm_request_type & 0x1F
    return (
        "ControlTransfer:"
        f"Direction=0x{direction:02x},"
        f"RequestType=0x{request_type:02x},"
        f"Recipient=0x{recipient:02x},"
        f"Request=0x{b_request:02x},"
        f"Value=0x{w_value:04x},"
        f"Idx=0x{w_index:04x},"
        f"Data={b64(payload_out)},"
        f"Length=0x{length:x}"
    )


def collect_devices(pcap: Path) -> tuple[dict[int, list[dict]], dict[int, dict]]:
    """Read the pcap once, pair S+C URBs, return per-devnum event lists +
    per-devnum descriptor info gleaned from any GET_DESCRIPTOR transfers."""
    pending: dict[int, dict] = {}  # urb_id → submit record
    events_per_dev: dict[int, list[dict]] = defaultdict(list)
    descriptor_info: dict[int, dict] = defaultdict(dict)

    for frame in iter_frames(pcap):
        rec = parse_usbmon(frame)
        if rec is None:
            continue
        if rec["xfer_type"] != XFER_TYPE_CONTROL:
            # We only care about control transfers (where SET/GET_REPORT live).
            # Bulk traffic could be added in a follow-up.
            continue
        if rec["urb_type"] == "S":
            pending[rec["urb_id"]] = rec
            continue
        if rec["urb_type"] != "C":
            continue
        submit = pending.pop(rec["urb_id"], None)
        if submit is None:
            continue
        if submit["setup"] is None:
            continue
        # IN transfers carry payload in the completion record; OUT transfers
        # carry it in the submission record.
        bm_request_type = submit["setup"][0]
        is_in = bool(bm_request_type & 0x80)
        wlen = struct.unpack("<H", submit["setup"][6:8])[0]
        if is_in:
            payload_out = b""
            response = rec["payload"][: rec["len_cap"]]
        else:
            payload_out = submit["payload"][: submit["len_cap"]]
            response = b""
        ev_id = event_id_for_control(submit["setup"], payload_out, wlen)
        evt = {"Id": ev_id}
        if is_in:
            evt["Data"] = b64(response)
        # Record the descriptor info from any GET_DESCRIPTOR transfer so we
        # can populate the device's vendor/product ID in the JSON snapshot.
        b_request = submit["setup"][1]
        w_value = struct.unpack("<H", submit["setup"][2:4])[0]
        if b_request == 0x06 and is_in and (w_value >> 8) == 0x01 and len(response) >= 14:
            # Standard GET_DESCRIPTOR(DEVICE) - 18 bytes total
            #   bLength, bDescriptorType, bcdUSB(2), bDeviceClass, bDeviceSubClass,
            #   bDeviceProtocol, bMaxPacketSize0, idVendor(2), idProduct(2), ...
            descriptor_info[submit["devnum"]]["VendorId"] = struct.unpack(
                "<H", response[8:10]
            )[0]
            descriptor_info[submit["devnum"]]["ProductId"] = struct.unpack(
                "<H", response[10:12]
            )[0]
        events_per_dev[submit["devnum"]].append(evt)

    return events_per_dev, descriptor_info


# ---------------------------------------------------------------------------
# Build the fwupd backend JSON snapshot (one phase blob).
# ---------------------------------------------------------------------------
def build_phase_json(
    devices: list[dict],
    fwupd_version: str = "2.0.0",
) -> str:
    obj = {
        "FwupdVersion": fwupd_version,
        "UsbDevices": devices,
    }
    return json.dumps(obj, indent=2)


def build_device_object(devnum: int, busnum: int, descriptor: dict, events: list[dict]) -> dict:
    """Construct a single UsbDevice JSON object suitable for fwupd's
    fu_backend_add_json reader."""
    dev = {
        "GType": "FuUsbDevice",
        # Backend ID — fwupd uses platform-id strings here. For USB devices
        # this is typically "BUS:DEV" or a sysfs path. Use BUS:DEV as a
        # stable identifier; replay just needs it to be consistent.
        "BackendId": f"{busnum}:{devnum}",
        "PhysicalId": f"USB\\VID_{descriptor.get('VendorId', 0):04X}&PID_{descriptor.get('ProductId', 0):04X}",
        "Created": 0,
        "VendorId": descriptor.get("VendorId", 0),
        "ProductId": descriptor.get("ProductId", 0),
        "Events": events,
    }
    return dev


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_vidpid(s: str) -> tuple[int, int]:
    a, b = s.split(":")
    return int(a, 16), int(b, 16)


# Default vid:pid pairs we keep when filtering. The U4025QW's main + secondary
# HIDs at firmware-mode + the bootloader-mode endpoints we observed in the
# captured update. Override with --vid:pid (multiple ok) for other monitors.
DEFAULT_KEEP_VIDPIDS = {
    (0x0BDA, 0x1100),
    (0x0BDA, 0x1101),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument(
        "--vidpid",
        action="append",
        default=[],
        help="VID:PID pairs to include (hex, e.g. 0bda:1100). Can be repeated. "
             "If unspecified, defaults to the U4025QW HIDs.",
    )
    ap.add_argument(
        "--keep-all",
        action="store_true",
        help="Keep transfers to/from every device address, not just the "
             "VID:PID-matched ones. Useful when unsure which device addresses "
             "are which.",
    )
    args = ap.parse_args()

    keep_vidpids = (
        DEFAULT_KEEP_VIDPIDS
        if not args.vidpid
        else {parse_vidpid(s) for s in args.vidpid}
    )

    print(f"reading {args.pcap} ({args.pcap.stat().st_size:,} bytes)…",
          file=sys.stderr)
    events_per_dev, descriptor_info = collect_devices(args.pcap)
    print(f"  {len(events_per_dev)} device(s), "
          f"{sum(len(e) for e in events_per_dev.values()):,} control transfers",
          file=sys.stderr)

    # Determine bus number of each devnum from the first frame we saw it on.
    # We've lost that during collection; re-do a cheap pass so the JSON has
    # a real BackendId.
    busnum_for_dev: dict[int, int] = {}
    for frame in iter_frames(args.pcap):
        rec = parse_usbmon(frame)
        if rec is None:
            continue
        busnum_for_dev.setdefault(rec["devnum"], rec["busnum"])

    devices_json = []
    for devnum, events in sorted(events_per_dev.items()):
        desc = descriptor_info.get(devnum, {})
        vid = desc.get("VendorId", 0)
        pid = desc.get("ProductId", 0)
        if not args.keep_all and (vid, pid) not in keep_vidpids:
            print(f"  skipping dev {devnum} ({vid:04x}:{pid:04x}) — not in keep list",
                  file=sys.stderr)
            continue
        print(f"  keeping dev {devnum} ({vid:04x}:{pid:04x}) — {len(events)} events",
              file=sys.stderr)
        devices_json.append(
            build_device_object(devnum, busnum_for_dev.get(devnum, 0), desc, events)
        )

    if not devices_json:
        sys.exit("no devices kept; aborting (try --keep-all to debug)")

    # For now produce a minimal emulation file: setup.json with the same
    # device descriptions but no events (just enumeration), and
    # install.json with the events. fwupd's emulation framework requires
    # at least setup.json to exist.
    setup_json = build_phase_json(
        [{**d, "Events": []} for d in devices_json]
    )
    install_json = build_phase_json(devices_json)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("setup.json", setup_json)
        zf.writestr("install.json", install_json)
    print(f"wrote {args.output} ({args.output.stat().st_size:,} bytes)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
