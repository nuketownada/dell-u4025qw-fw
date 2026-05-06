# dell-monitor-rt — fwupd plugin design notes

> Working title: `dell-monitor-rt` (Dell monitors with RealTek scaler controllers,
> updated via Wistron's HID protocol used by Dell Display & Peripheral Manager).

## At-a-glance (could become the plugin README intro)

`dell-monitor-rt` is a Linux Vendor Firmware Service (LVFS) / `fwupd` plugin
that updates firmware on Dell external monitors built around a RealTek scaler
controller and Wistron-authored "ISP" (in-system programming) firmware. Today,
firmware for these monitors can only be applied via Dell Display & Peripheral
Manager (DDPM) on Windows or macOS, or by running Dell's proprietary
`monitorfirmwareupdateutility-*.deb` package on Ubuntu. This plugin makes the
update available to any Linux distribution via `fwupdmgr`, with the firmware
binary either supplied directly by the user or distributed through LVFS once
Dell consents.

The reference monitor for this work is the **Dell U4025QW** (40", 5K2K, USB-C
upstream, Realtek scaler), but the same protocol family is used on a number of
Dell monitors that ship Wistron's updater (P/U-series, several built-in-dock
SKUs). Adding new monitor models should be a matter of declaring USB IDs and
updating a quirk table.

### Why this plugin exists

- Dell has not yet contributed these monitors to LVFS.
- The current alternatives all break in practice for many users:
  - DDPM is Windows/Mac only.
  - The Ubuntu `.deb` is a GTK GUI tied to `libsciter` and assumes a desktop
    session — awkward to run on headless or non-Debian systems.
  - The protocol is a proprietary Wistron HID command set; nothing in
    upstream `fwupd` covers it (`realtek-mst` covers a different RealTek
    chip family, `dell-dock` covers TI/Synaptics dock chips, `mediatek-scaler`
    covers MediaTek scalers used in Dell AIOs).
- The .deb shows the protocol is small and well-structured; clean
  reimplementation is realistic.

### Scope

In scope:

- Read currently-installed firmware version from the monitor.
- Validate that a given `.upg` payload is intended for this monitor
  (model name, panel ODM string, header version).
- Walk the monitor through the standard Wistron flash sequence:
  enter bootloader → erase target bank → write payload → verify → exit
  bootloader → reboot.
- Surface progress to `fwupd` so frontends (Software, GNOME Firmware, etc.)
  show a sensible progress bar.
- Refuse downgrades by default; respect a user-set "force" flag.

Out of scope (at least initially):

- Updating peripherals attached *to* the monitor (touch panel, optional
  Dell C-series webcam dock, the built-in Realtek 2.5G ethernet) — Dell's
  .deb includes plugins for these as separate components; we'll add them
  as separate fwupd device children once the scaler path is solid.
- Updating monitor *settings* (brightness, color profiles) — that's
  `ddcutil`'s job, not fwupd's.

### High-level architecture

```
┌──────────────┐  HID SET_REPORT (192-byte Output) ┌────────────────────┐
│              │ ────────────────────────────────► │                    │
│ fwupd plugin │                                   │ Realtek scaler MCU │
│  (this code) │ ◄──────────────────────────────── │  in monitor's hub  │
│              │  HID GET_REPORT (192-byte Input)  │                    │
└──────┬───────┘                                   └─────────┬──────────┘
       │                                                     │
       │ control transfer to interface 0 of                  │ I²C
       │ /dev/bus/usb/<bus>/<addr>                           ▼
       │                                           ┌────────────────────┐
       │                                           │ Panel TCON / DSP / │
       │                                           │   peripheral MCUs  │
       │                                           └────────────────────┘
       │
   ID match: 0bda:1100 + 0bda:1101 (the two HID interfaces the
   monitor's USB-upstream hub exposes for management)
```

Key facts that drive the implementation:

- **Transport is USB HID**, but uses control transfers (SET_REPORT /
  GET_REPORT class requests on EP0), not the interrupt endpoints.
- **All requests and responses are exactly 192 bytes**, regardless of opcode.
- The first byte selects a command class (`0x40` = write/action,
  `0xc0` = read/status); the second byte is a subcommand; bytes 2–7 are
  arguments; the remaining 184 bytes are payload.
- The protocol exposes both **direct flash operations** (write block, erase
  bank, etc.) and an **I²C-tunnel** (`HID_ReadI2C` / `HID_WriteI2C` in
  `librtburn.so`) which the monitor's MCU forwards to internal busses to
  reach the panel TCON and other component MCUs. We probably only need
  the direct flash path for the scaler firmware itself; the I²C tunnel
  matters once we expand to peripherals.

---

## What we know

### 1. Hardware identification

Reference device:

- **Model**: Dell U4025QW (40" 5K2K curved, USB-C upstream).
- **EDID manufacturer**: `DEL` / model `0x4308` / "DELL U4025QW".
- **Internal scaler controller** (per DDC/CI VCP `0xC8`):
  Mfg = RealTek, controller number `0x002738`.
- **Current firmware level reported via DDC/CI VCP `0xC9`**: `65.5`
  (does not change between Dell's "M3T103/104/105" external versions —
  the VCP value is the controller's internal revision string, not the
  same numbering Dell uses publicly).

Panel-ODM split:

- The U4025QW ships with multiple panel suppliers depending on production
  batch (LG Display, AU Optronics, BOE, CSOT have been reported).
- Dell ships **panel-ODM-specific `.upg` files**. The filename encodes
  the panel ODM (e.g., `DELL_U4025QW_LGD_4FCF2_M3T105_20251009.upg` =
  LG Display panel).
- The reference monitor used here has an **LGD panel** (the LGD `.upg`
  was accepted and successfully reflashed).
- Plugin must reject mismatched payloads with a clear error rather than
  brick the monitor.

### 2. Firmware payload (`.upg`) format

Length-prefixed records, big-endian 32-bit lengths:

```
[u32 BE: 3]  "UPG"                         # magic
[u32 BE: 5]  "1.0.6"                       # format version
[u32 BE: 7]  "U4025QW"                     # product code
[u32 BE: 6]  "M3T105"                      # firmware version
[u32 BE: 6]                                # component count (= 6 on this file)
[u32 BE: 4]  "HUB1"                        # component name
[u32 BE: 4]  "HUB2"
[u32 BE: 4]  "HUB4"
[u32 BE: 3]  "HUB"
[u32 BE: 3]  "PDC"
[u32 BE: 7]  "DISPLAY"
[u32 BE: 1]  0x00000000                    # ?
[u32 BE: 7]  "DISPLAY"                     # appears twice — two scaler images?
[u32 BE: 7]                                # 7 ASCII signature blocks follow
[u32 BE: 96] "`<base64-url-safe sig 96B>"  # repeated per component, each
... (one per component) ...                # block prefixed by `0x60` length
                                           # and a leading backtick byte.
[ binary firmware payload bodies ]         # remaining bytes (~1.1 MB total)
[ trailing record ]
[u32 BE: 16] "UPDATE_BY_SCALER"            # delivery method tag
[u32 BE: 1]  0x00000000
[u32 BE: 96] "`<final 96B sig>"            # closing signature
```

Open questions about the format are listed in §"What we still need to learn".

Cert files shipped alongside the .upg in the .deb:

- `cert.dat` — 18 bytes (looks like a hash or short ID, not yet decoded)
- `cert2.dat` — 26 bytes ASCII (also short identifier)
- `appconfig.dat` — 813 bytes encrypted blob (purpose unknown)

The protocol verifies signatures on-device via `librtburn.so::hid_CheckECDSA`.
The 96-byte ASCII blocks in the `.upg` are the right size for raw-encoded
SHA-384 ECDSA signatures.

#### Verification model: chip-side, confirmed (2026-05-02)

Verified empirically against `captures/u4025qw-update-recap-20260502-185321.pcapng`
(a fresh full-update capture on usbmon3): the chip's bank-switch / commit
code performs the signature verify itself, NOT the host. Smoking gun is
a **full second of zero traffic on the monitor's HID device** between the
last `SET_REPORT` of the bulk-write phase and the final commit-trigger
`SET_REPORT`:

```
+0.000s  SET_REPORT  40 c6 00 00 00 00 02 00 94 00 01 00 …  (write block N)
+0.001   SET_REPORT  40 d6 6f 00 00 00 01 00 94 01 01 00 …  (commit-ready signal)
+0.001   GET_REPORT  ← chip OK
+0.000   (1.000 SECONDS OF SILENCE on this device — chip computes verify)
+1.000   SET_REPORT  40 c6 00 00 00 00 02 00 94 00 01 00 … ee 02   (commit / reset trigger)
+1.001   SET_REPORT_RESPONSE                                        (instant ack — chip already done)
+1.077   URB_INTERRUPT IN                                           (disconnect — chip rebooting)
```

If the host were doing a host-side verify (reading the staged bank back over
HID and ECDSA-verifying locally) we'd see hundreds-of-thousands of bulk-read
frames in that 1-second window. There are zero. The chip is doing all the
crypto internally. ~1 second is consistent with secp384r1 ECDSA on a slow
hub MCU CPU.

That makes `librtburn.so::hid_CheckECDSA` a thin host wrapper: it issues a
short vendor command (almost certainly the `40 c6 …` or `40 d6 …` SET_REPORT
in the dense pre-gap region) and reads the chip's status byte back via
GET_REPORT. The host never sees the firmware bytes after they're written
to the chip's flash bank.

**Implications for the fwupd plugin:**

* The brick risk during plugin development is much lower than worst-case —
  the chip will reject our firmware if signature verification fails (taking
  the ~1s pause and then NOT swapping banks), rather than silently
  committing garbage and bricking on next boot. We still want the
  emulation-replay safety net (see "Pre-flight emulation harness" below)
  but it's belt-and-suspenders, not life-or-death.
* The plugin doesn't need to do its own signature check before sending
  bytes — the chip does that. We just need to honour the protocol's
  multi-stage flow: stream blocks → wait for chip's "commit-ready" status
  → issue the trigger → expect a reset/disconnect → reconnect to the
  rebooted device.
* Real chain of trust is anchored in chip ROM (whatever signing pubkey
  RealTek burns into the RTS5409S at fab — not recoverable by us, not
  needed by us), separate from Dell's per-bundle wrapper signature on
  the .upg payload. Dell's wrapper is for transport integrity; the chip's
  on-device ECDSA is what protects against actual flash tampering.

### 3. USB / udev

The U4025QW exposes two HID-class interfaces relevant to firmware update on
its built-in USB hub:

| Vendor | Product | Role |
|--------|---------|------|
| `0bda` | `1100`  | RealTek "USB2.0 HID" (one of two firmware-update endpoints) |
| `0bda` | `1101`  | RealTek "USB2.0 HID" (the other) |

The .deb's udev rules list ~70 USB IDs total, covering the monitor itself
plus all attachable Dell C-series webcam SKUs. For just the scaler firmware
update path, only the two IDs above are required.

The .deb relies on `MODE:="0666"` to let an unprivileged GUI user open the
device. fwupd's daemon runs as root and does not need that — for a fwupd
plugin we should ship udev rules that simply **tag the device** so the
daemon discovers it:

```
ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="1100", TAG+="uaccess", \
  ENV{ID_FWUPD_PLUGIN}="dell_monitor_rt"
ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="1101", TAG+="uaccess", \
  ENV{ID_FWUPD_PLUGIN}="dell_monitor_rt"
```

These rules ship in the plugin's package, alongside the `.so`, and are
auto-installed by Nix via `services.udev.packages = [ … ]` /
`services.fwupd.extraPackages = [ … ]`.

### 4. The wire protocol

#### Frame layout

All transfers are exactly **192 bytes** of payload, carried as HID class
SET_REPORT / GET_REPORT requests on control endpoint 0:

```
SETUP packet:
  bmRequestType  0x21  (host→device, class, interface)   ; SET_REPORT
                 0xa1  (device→host, class, interface)   ; GET_REPORT
  bRequest       0x09  (SET_REPORT) / 0x01 (GET_REPORT)
  wValue         0x02XX  (ReportType=Output(2), ReportID=XX)
  wIndex         0x0000  (interface 0)
  wLength        0x00C0  (192)
```

Payload (192 bytes):

```
offset  size  field
   0     1    cmd_class   0x40 = write/action,  0xc0 = read/status
   1     1    subcmd
   2     6    args (subcmd-specific; addresses, lengths, flags)
   8   184    data (firmware bytes for write commands; zero-padded
              for control-only commands)
```

#### Observed opcodes (from a single full update cycle, 17.95 min)

From device with USB address 31 (one of `0bda:1100`/`1101` after
re-enumeration into bootloader mode):

| Count   | First 4 bytes      | Probable role |
|--------:|--------------------|--------------|
| 87,352  | `c0 f3 00 00`      | Status poll (tight loop) — see "0xC0 F3 status poll" below |
|  4,077  | `40 c6 00 00`      | TBD — likely version/info or bootloader-enter |
|  2,589  | `40 d6 60 00`      | `0xd6` subcommand family (probably erase-related) |
|  1,840  | `40 d6 08 00`      | (same family) |
|  1,832  | `40 d6 09 00`      | (same family) |
|  1,607  | `40 d6 6f 00`      | (same family) |
|     27  | `40 d6 75 00`      | (same family) |
|     26 each × 16  | `40 f1 80 f0..ff` | **Flash write loop**, sweeping low addr byte |
|     26  | `40 f5 94 01`      | TBD |
|     26  | `40 f4 00 00`      | TBD |

#### 0xC0 F3 status poll

The tightest-loop traffic in the entire update — 87,352 polls in the
captured pcap, ~2 ms between consecutive polls. Issued by the host
between every byte-write to the bootloader-mode device. Decoded by
pairing each request with its response across the entire pcap (script
in commit history; one tshark pass + state-machine pairing).

Wire format:

```
request   c0 f3 00 00 00 00 01 00 [184 bytes of zero]    SET_REPORT
response  XX 00 00 00 00 00 00 00 [184 bytes of zero]    GET_REPORT
```

The request is fixed — the only variable is the response's first byte.
All 192 trailing bytes of the response are always zero.

Status byte values observed:

| Byte 0 | Meaning             | Frequency           |
|-------:|---------------------|---------------------|
| `0xA0` | READY — proceed     | 88,787 / 88,813 (99.97%) |
| `0x00` | BUSY — poll again   |     26 / 88,813 (0.03%) |

Every BUSY response in the pcap fell at the same protocol point: the
host had just sent the block-finalize sequence
(`40 f1 80 …, 40 f1 00 …, 40 f1 80 …, 40 04 01 …, 40 f5 94 …`) which
asks the chip to commit a 256-byte block. The chip needs a few extra
milliseconds to compute its CRC and advance the bank pointer; for that
brief window the next status poll returns `0x00`. As soon as the
commit lands, status flips back to `0xA0` and the host writes the
next block.

Implementation note for the plugin: the simplest correct read is
"poll until byte 0 != 0x00". Cadence in Dell's binary is ~2 ms; the
chip never needed more than a handful of polls (max BUSY-streak in the
pcap is 1 — i.e. each block-commit cleared on the next poll). A 1 ms
poll-interval is plenty of headroom; 5 ms is also fine.

There's no "error" status seen in this pcap — every operation
succeeded. We don't yet know what an error response looks like (some
other byte 0 value? A different second byte? A non-zero tail?), so
the plugin should treat any unexpected non-`0x00`/`0xA0` value as a
hard failure rather than assume more semantics.

#### 0x40 C8 — host-to-device firmware staging (pre-bootloader)

The 1,536 c8 frames in the pre-bootloader phase aren't reads — they're
**SET_REPORT writes** that load 8051 firmware into the hub MCUs ahead
of the bootloader-entry trigger. The "1,536 = 3 passes × 256 addresses
× 2 flag bits" count from the earlier table is structurally correct,
but the operation is a firmware *load*, not a header *read*. The
COMPLETE frame for each c8 SET_REPORT carries no payload — the device
just acks.

Wire layout per frame:

```
offset  0..7    8 bytes   header:  40 c8 <FLAG> <ADDR> 00 00 80 00
offset  8..63   56 bytes  zero padding
offset 64..191  128 bytes 8051 firmware code (the actual payload)
```

Header fields:

| Field | Position | Meaning |
|---|---|---|
| `0x40`         | byte 0 | DIR_WRITE |
| `0xc8`         | byte 1 | "load firmware chunk" opcode |
| flag (`0x00` / `0x80`) | byte 2 | which half of the 256-byte block: `0x00` = low 128, `0x80` = high 128 |
| addr (`0x00`-`0xff`)   | byte 3 | 256-byte block index — 256 blocks → **64 KB per pass** |
| `0x80`         | byte 6 | payload length (= 128 bytes following at offset 64) |

Reconstructed traffic in the recap pcap:

| Device | c8 frames | Total bytes loaded | Pattern |
|---|---:|---:|---|
| dev 61 (DEV_1101 secondary HID, firmware mode) | 1,024 | 128 KB | 256 addr × 2 halves × **2 passes** |
| dev 63 (DEV_1100 primary HID, firmware mode)   |   512 |  64 KB | 256 addr × 2 halves × **1 pass**  |
| dev 71 (DEV_1100 primary, **bootloader mode**) |     0 | —      | none — c8 traffic is pre-bootloader only |

Concatenating bytes 64..191 from successive c8 frames in (addr, flag)
order produces a contiguous 8051 binary. Sample first 32 bytes of the
dev 63 blob:

```
02 08 da 02 20 81 02 20 7a 22 ff 02 77 4c 02 20 7a ff ff 02 77 dd ff ff ff ff ff 02 99 21 f9 d1
```

Those leading `02 XX YY` triples are 8051 LJMP instructions — a
classic interrupt vector table (reset vector → 0x08DA, then external
INT0/timer/serial/etc handlers). This is unambiguously executable
8051 firmware code, almost certainly the **ISP shim** the host loads
into the hub MCU's RAM ahead of the bootloader-entry trigger so the
post-trigger MCU has flash-write code to jump into.

Plugin implications:

1. The c8 staging phase **must run before the 0xE9 trigger** for the
   captured-pcap protocol to work. Skipping it leaves the bootloader
   with no ISP code to execute. (Unverified hypothesis — we haven't
   yet tested whether the chip's own bootloader has resident
   ISP code or strictly relies on the host-loaded version.)
2. The data **is the .upg's `HUB` and `HUB4` components, decrypted**
   — bytewise identical to what `fwupdtool firmware-extract` against
   the .upg with our plugin emits. The initial raw-byte grep across
   the on-disk `extracted/` tree found no match because the .upg
   stores these components encrypted (per-component ECIES + passphrase
   wrapping); the wire data is what falls out after decryption.

   **Updated mapping (2026-05-05) — all four pre-bootloader blobs are
   ephemeral RAM-staged ISP shims, one per MCU.** The 9,717-event
   "skip" on HID-A pre-bootloader (between version probe and the first
   c8 frame on the next device incarnation) turns out to stage two
   *additional* ISP shims onto downstream MCUs over the i2c tunnel —
   bytewise sha256-equal to the decrypted `HUB1` and `HUB2` components.
   Verified against `id-HUB1.fw` / `id-HUB2.fw` from
   `fwupdtool firmware-extract`:

   | Component | Size   | Target              | Wire path                                 | Frame count                       |
   |-----------|-------:|---------------------|-------------------------------------------|-----------------------------------|
   | `HUB1`    | 128 KB | i2c slave `0xD4`    | HID-A, opcode `0xC6` (i2c tunnel WRITE)  | 2048 frames × 64 B 8051 chunks    |
   | `HUB2`    |  64 KB | i2c slave `0xD6`    | HID-A, opcode `0xC6` (i2c tunnel WRITE)  | 1024 frames × 64 B 8051 chunks    |
   | `HUB`     |  64 KB | DEV_1100 (primary)  | HID-A, opcode `0xC8` (RAM stage)         | 512  (1 pass × 256 × 2)           |
   | `HUB4`    | 128 KB | DEV_1101 (secondary)| HID-B, opcode `0xC8` (RAM stage)         | 1024 (2 passes × 256 × 2)         |

   The 0xC6 frames carry a fixed `13 40` i2c sub-command header at
   payload offset 64, followed by 64 bytes of 8051 firmware per frame.
   Each cycle of writes is gated by a fresh cal_auth handshake
   (`40 e1 01 01` → GET_REPORT challenge → `40 e1 03 00` response),
   then `40 06 00` (disable high clock), `40 02 01 00 da 0b` (re-enable
   vdcmd), `40 06 01` (re-enable high clock). The captured trace runs
   that refresh **56 times** across the bulk transfer.

   **Why we believe all four are ephemeral, not flash:**
   - All four blobs are exact powers of two (64 KB / 128 KB) with **no
     trailing bytes for an in-band signature**. If any of them were
     being persisted to flash, you'd expect either a sig trailer or an
     explicit verify opcode, neither of which is on the wire.
   - The protocol shape is symmetric **stage → stage → stage → stage →
     trigger**: four loads of executable 8051 code into four MCUs'
     RAM, then a single `0xE9` to the USB-connected primary chip.
     The primary's now-running ISP shim then orchestrates the
     downstream MCUs whose ephemeral payloads are already sitting in
     their RAM, ready to run.
   - `cal_auth` is host→chip authentication (cal_auth's response
     depends only on the chip's challenge nonce and a static 8-byte
     key, never on firmware bytes — see "cal_auth decoded" above), not
     a firmware-integrity check. The chips' acceptance gate is "did
     you pass cal_auth?" and nothing more.

   **Integrity model:** the entire chain of trust is anchored
   *host-side*, in Dell's binary verifying the .upg's per-component
   ECDSA trailer with the embedded Wistron pubkey before any wire
   activity. We replicated that check in tasks #24/#25. The chips
   themselves trust whatever is staged via cal_auth-gated transport.
   The persistent flash writes happen *post*-bootloader via `0x40 F1`
   (see below), driven by the now-running ISP shims rather than by
   the host directly.

   The remaining components (`PDC`, `753.0AK01.0007`) are presumed to
   be the actual persistent firmware payloads streamed through the
   running ISPs in the post-bootloader bulk phase.

Sample real packet payloads (first 16 bytes, frame numbers from the
captured pcap):

```
70866   40 02 01 00 da 0b 00 00 …
70868   40 06 01 00 00 00 00 00 …
70870   40 e1 01 01 00 00 00 00 …
70874   40 e1 03 00 00 00 00 00 …
70876   c0 ca 00 00 00 00 20 00 …   ; cmd-class 0xc0 = read
70878   40 e1 01 01 00 00 00 00 …
70882   40 e1 03 00 00 00 00 00 …
70884   40 06 00 00 00 00 00 00 …

435330  40 f1 80 7d 00 00 80 00 [184 bytes of real firmware data]
435334  40 f1 00 7e 00 00 80 00 [184 bytes of firmware data]
435336  40 f1 80 7e 00 00 80 00 [184 bytes of firmware data]
435340  40 f1 00 7f 00 00 80 00 [184 bytes of firmware data]
…
```

Pattern: `40 f1 <flag:80|00> <block_lo>` is the flash-write opcode. Each
block address sees a pair of writes (`flag=0x00` then `flag=0x80`), each
delivering 184 bytes — i.e., 368 bytes per (logical) flash block. Total
write count (~6,000) × 184 bytes ≈ 1.1 MB, matching the `.upg` payload
size.

#### Mapping opcodes to `librtburn.so` symbols

`librtburn.so` is shipped unstripped; the symbols below are likely
candidates to map onto observed opcodes:

| Symbol                          | Likely opcode |
|---------------------------------|---------------|
| `hid_GetUSBUpdateFWProgress`    | `c0 f3 00 00` poll |
| `hid_fwUSBBootloader`           | one of `40 e1 01 01`, `40 e1 03 00`, `40 06 ..` |
| `hid_GetUSBFwVersion`           | one of the `c0 ..` reads |
| `hid_fwUpdateUSB`               | `40 f1 …` write loop driver |
| `EraseBank` / `script_EraseBank_All` | `40 d6 ..` family |
| `script_EnProtectAll` / `script_UnProtectAll` | TBD `40 ..` opcodes |
| `hid_CheckECDSA`                | `40 02 01 00 …` (signature pass) |
| `hid_fwReboot`                  | one of `40 06 ..` likely (last command before reset) |
| `WRITEDATABUF` / `INVERT_4BYTE` | helpers, not standalone opcodes |

The next concrete step toward a working plugin is to do a one-time
disassembly pass on `librtburn.so` and confirm each of these mappings
from the captured byte sequences.

### 5. fwupd integration sketch

A first-cut plugin layout:

```
plugins/dell-monitor-rt/
├── README.md
├── meson.build
├── fu-plugin-dell-monitor-rt.c          ; FuPluginVfuncs entry points
├── fu-dell-monitor-rt-device.c          ; FuHidDevice subclass
├── fu-dell-monitor-rt-device.h
├── fu-dell-monitor-rt-firmware.c        ; FuFirmware subclass for .upg parsing
├── fu-dell-monitor-rt-firmware.h
├── 99-dell-monitor-rt.rules             ; udev tags
└── tests/                               ; sample .upg headers, opcode fixtures
```

Device subclass responsibilities:

1. Probe — detect `0bda:1100`/`0bda:1101` and present as one logical device.
2. `setup()` — open HID, send `hid_GetUSBFwVersion`, populate version.
3. `prepare()` — `hid_fwUSBBootloader` (transition to flash mode).
4. `write_firmware()` — call `EraseBank`, then loop `hid_fwUpdateUSB`
   over 184-byte chunks of the parsed `.upg` payload, polling
   `hid_GetUSBUpdateFWProgress` to drive `fu_progress_set_percentage()`.
5. `cleanup()` — `script_EnProtectAll`, then `hid_fwReboot`.
6. `attach()` — wait for the device to re-enumerate after reboot and
   confirm new version reads back.

Firmware subclass parses the length-prefixed `.upg` (see §2), exposes:
- `fu_firmware_get_version()` (e.g., `M3T105`)
- `fu_firmware_get_id()` (e.g., `U4025QW-LGD`)
- per-component children for `HUB1`/`HUB2`/`HUB4`/`HUB`/`PDC`/`DISPLAY`
- ECDSA signature verification slot (when we know which key).

LVFS metadata (`metainfo.xml`) once we're ready to publish:

- `<provides><firmware type="flashed">guid-…</firmware></provides>`
- A `<requires>` clause keyed off the panel ODM string so users only see
  payloads that match their panel.

### 6. NixOS packaging

For interim local use (run Dell's binary), the udev rules from the .deb
have been added to `machines/signi/configuration.nix` under
`services.udev.extraRules` so any user can run the GUI updater. Once the
fwupd plugin replaces the need for the .deb, those rules get **removed
from the machine config** and ship inside the plugin's own package as
discussed above.

For the eventual plugin package, expected NixOS surface:

```nix
mynix.fwupd-plugin-dell-monitor-rt = pkgs.stdenv.mkDerivation {
  pname = "fwupd-plugin-dell-monitor-rt";
  version = "0.1.0";
  # …
  installPhase = ''
    install -Dm755 libfwupd_dell_monitor_rt.so \
      $out/lib/fwupd-${pkgs.fwupd.version}/plugins/libfwupd_dell_monitor_rt.so
    install -Dm644 99-dell-monitor-rt.rules \
      $out/lib/udev/rules.d/99-dell-monitor-rt.rules
  '';
};

# In machine config:
services.fwupd.extraPackages = [ pkgs.mynix.fwupd-plugin-dell-monitor-rt ];
services.udev.packages       = [ pkgs.mynix.fwupd-plugin-dell-monitor-rt ];
```

### 7. fwupd policy considerations

- **Plugin code**: must be LGPL-2.1-or-later, written from scratch
  (reverse-engineered protocol is fine; do not link against
  `librtburn.so` for upstream submission).
- **Firmware payload**: redistribution requires Dell consent. Until
  then, the plugin reads `.upg` from a path the user provides
  (extracted from Dell's .deb).
- Reverse-engineered plugins are explicitly welcomed by the fwupd
  project (precedent: System76 EC, several SteelSeries devices,
  initial Logitech Unifying support).

---

## What we still need to learn

These are the gaps between "we have a clear protocol picture" and "we can
ship a plugin." Each section gets filled in as work progresses.

### Disassembly findings — protocol architecture is layered C++

Initial assumption ("the protocol is in `librtburn.so`") was **wrong**.
Disassembly revealed the real picture:

#### `librtburn.so` is for the Dell webcam attachment, not the monitor

- `hid_GetUSBFwVersion` dispatches on VID `0x413c` (Dell) and PIDs `0xc068`/
  `0xc06c` — those are the Dell C-series webcam attachment, not the U4025QW
  hub. So `librtburn.so` flashes the *peripheral* via direct USB HID,
  not the scaler.
- Confirmed opcodes from `librtburn.so` for the webcam path:
  - `hid_fwGetVersion`: sends `<id> 10 00 …` (8-byte report)
  - `hid_fwUSBBootloader`: sends `<id> 02 00 …` then closes the device
  - `hid_fwUpdateUSB`: sends `<id> a2 00 …` to begin the update
  - `hid_fwReboot`: sends `<id> a8 00 …`
  - `HID_WriteI2C` (I²C tunnel for *the webcam chip*): sends
    `00 00 57 <subcmd> <len> <data…>`  ('W' = 0x57)
- None of these opcodes match anything seen on the wire during the
  U4025QW update. Confirms `librtburn.so` is a different code path.

#### The U4025QW path is in `libhub.so`, layered C++

```
plugins/libhub.so:
  ┌─ FL5500_IIC_ISP            high-level: update_hub, isp, terminate_isp,
  │    └── FL5500_IIC_API           mid-level: read_byte, write_byte,
  │                                  write_burst, bit_polling,
  │                                  set_sram_*, sram_to_spi_flash,
  │                                  trigger_write_spi_flash, …
  │           └── IIC_INTF              abstract I²C interface base class
  │                  ├── Rts5409s_IIC_ISP    ← concrete: RealTek RTS5409S USB hub
  │                  ├── Rts5418e_ISP            (other RealTek hub model)
  │                  ├── Mchp58xx_72xx_ISP       (Microchip USB hubs)
  │                  ├── PS5512_IIC_ISP          (PS5512 hub)
  │                  └── …
  │
  └─ RTS5409s_IIC_API           hub-MCU-specific ops: write_flash,
                                 read_fw_version, polling_status,
                                 verify_fw, soft_reset, clear_address
```

- **`FL5500`** is the RealTek scaler IC family inside the monitor.
- **`RTS5409S`** is the upstream USB hub MCU inside the monitor; it's
  the device that exposes `0bda:1100`/`1101` and acts as the I²C bridge.
- The host talks HID-class control transfers to the RTS5409S, which
  forwards I²C transactions to the FL5500 scaler over an internal bus.
- The 192-byte HID Output Reports we observed are
  **I²C transactions tunnelled through HID**, with the leading byte
  serving as the direction marker (`0x40` = host writes I²C, `0xc0` =
  host reads I²C status).
- The 13,312-call write loop `40 f1 80 XX` we observed corresponds to
  the SRAM-staged flash write pattern in
  `FL5500_IIC_API::sram_to_spi_flash` — the host writes 184-byte chunks
  into the FL5500's SRAM via I²C, then triggers the SRAM→SPI-flash copy.
- The 87,352 `c0 f3` polls correspond to
  `RTS5409s_IIC_API::polling_status` (also called by `FL5500_IIC_API::
  bit_polling` over I²C).

#### Implications for the fwupd plugin design

The plugin should mirror the real layering:

```
fu-dell-monitor-rt-device.c       (FuHidDevice subclass)
   │   - opens /dev/hidraw matching 0bda:1100/1101
   │   - exposes plain SET_REPORT/GET_REPORT helpers
   │
   ├─ fu-iic-tunnel.c             (HID-encoded I²C transport)
   │   - encodes I²C ops as 192-byte Output Reports
   │     (0x40 prefix = I²C write, 0xc0 = I²C read)
   │   - matches Rts5409s_IIC_ISP on the device side
   │
   ├─ fu-rts5409s-flash.c         (hub-MCU flash ops if we ever update it)
   │   - write_flash, polling_status, soft_reset, …
   │
   └─ fu-fl5500-isp.c             (scaler ISP — the main payload path)
       - SRAM staging: set_sram_page, set_sram_start_addr,
         write_burst (host→SRAM)
       - flash trigger: set_spi_flash_start_addr,
         set_spi_flash_transfer_size, trigger_write_spi_flash
       - completion: bit_polling on done bit, verify_fw
       - high-level: update_hub() = the full erase→write→verify flow
```

This is *cleaner than initially expected*: `realtek-mst` (existing fwupd
plugin) already implements similar SRAM-staged SPI flash logic for
RTD2141B/RTD2142 over the DisplayPort aux channel. We can crib the
flash-protocol structure from there and just swap the transport
(DP-aux → HID class control transfers).

#### Source tree, recovered from debug paths

The plugins ship with debug info that reveals Wistron's source
organisation:

```
/home/vsts/work/1/s/                    ; Azure DevOps build root
├── devices/
│   ├── microchip_pic16f1454/{pic16f1454_hid.cpp, pic16f1454_iic.cpp}
│   ├── microchip_usb5734/{usb5734_hid.cpp, usb5734_iic.cpp}
│   ├── microchip_usb5916/{…}
│   ├── microchip_usb7206/{usb7206_hid.cpp, usb7206_iic.cpp,
│   │                     usb7206_vcmd.cpp}                ; vendor-cmd helpers
│   ├── ti_tusb3410/{tusb3410_hid.cpp, tusb3410_iic.cpp}
│   ├── ti_tusb8043/{…}
│   └── realtek_rts5409s/{rts5409s_hid.cpp, rts5409s_iic.cpp}
├── hub/
│   ├── realtek_rts5418e_usb/rts5418e_isp.cpp
│   ├── microchip_usb58xx_usb72xx_usb/mchp58xx_72xx_isp.cpp
│   └── microchip_usb72xx_usb_v3/mchp_usb72xx_v3_isp.cpp
└── parade_ps5512/ps5512_vdcmd.cpp
```

Translation to plugin layout:

| Plugin | Notable classes | Purpose |
|--------|----------------|---------|
| `libdevices.so` | `RTS5409S_HID`, `RTS5409S_IIC`, `USB5734_HID`, `TUSB3410_IIC`, `PS5512_HID`, `PIC16F1454_IIC`, `USB5916_IIC`, … | Per-chip USB/HID transports + per-chip I²C tunnels (the actual wire-bytes layer) |
| `libhub.so` | `FL5500_IIC_ISP`, `Rts5409s_IIC_ISP`, `Mchp58xx_72xx_ISP`, `Mchp72xx_V3_ISP`, `Rts5418e_ISP`, `PS5512_IIC_ISP` | High-level ISP orchestrators for hub MCUs and the scaler |
| `libdisplay.so` | `MStarISP`, `RealtekISP`, `MSTAR_API` | Display panel TCON / scaler updates (alternative path) |
| `libpdc.so` | `RTS545X_ISP`, `TitanRidge_ISP`, `Tps6598xISP` | USB-PD controllers |
| `libtbt.so` | `GoshenRidge_ISP`, `TitanRidge_ISP` | Thunderbolt controllers |
| `libwebcam.so` | `SigmaStar_Camera_ISP` | Webcam ISP |
| `libtouch.so` | `FlatFrog_Touch_ISP` | Touch panel |
| `libaudio.so` | `ALC4058_ALC5576_ISP`, `CX20889_ISP` | Audio codecs |

The U4025QW's specific update path goes through:
- `libhub.so:FL5500_IIC_ISP` (high-level)
   ↓ injected via `set_i2c_handler(IIC_INTF*)`
- `libdevices.so:RTS5409S_IIC` (concrete `IIC_INTF`)
   ↓ wraps
- `libdevices.so:RTS5409S_HID` (libusb-hidapi transport)
   ↓ sends
- `libusb_control_transfer` → SET_REPORT class request

#### Wire format — fully decoded

Confirmed by disassembling `RTS5409S_HID::enable_vdcmd(bool, bool)` and
matching against the captured frame 70866 of the pcap.

**libusb call**:
```
bmRequestType = 0x21              ; host→device, class, interface
bRequest      = 0x09              ; SET_REPORT
wValue        = 0x0200            ; ReportType = Output (2), ReportID = 0
wIndex        = 0x0000            ; interface 0
wLength       = 0x00C0            ; 192 bytes
```

`hid_write` is called with a 193-byte buffer; because ReportID = 0, the
leading byte is stripped on the wire, leaving the 192-byte payload below.

**192-byte payload**:
```
offset  size  field         meaning
   0     1    direction     0x40 = WRITE (host→device data flow)
                            0xC0 = READ-back (host expects subsequent IN)
   1     1    opcode        vendor command opcode
                            ──────────────────────
                            0x02  enable_vdcmd      (vendor-cmd mode toggle)
                            0x06  enable_high_clock (paired with enable_vdcmd; sub=01
                                                      to enable, sub=00 to close)
                            0x09  get_self_fw_version (hub MCU's *own* firmware
                                                      revision; READ via HIDIOCGINPUT.
                                                      Verified working in plugin —
                                                      returns "%X.%02X" hex format.)
                            0xC6  I²C tunnel WRITE   (sends bytes onto the monitor's
                                                      internal I²C bus to a 7/8-bit
                                                      target — usually DDC/CI 0x6E,
                                                      sometimes 0x94 for the FL5500.
                                                      Gated by the 0xE1 handshake;
                                                      see "I²C tunnel" subsection.)
                            0xC8  flash read         (256 addresses × 2 flags × 3 passes
                                                      = the 1,536 reads at start)
                            0xD6  I²C tunnel READ /  (companion to 0xC6, also gated
                                  status poll family  by the 0xE1 handshake)
                            0xE1  auth handshake     (challenge/response — required
                                                      before the device will accept any
                                                      0xC6 / 0xD6 traffic; sub 0x01
                                                      requests challenge, sub 0x03
                                                      sends 8-byte response. Algorithm
                                                      not yet decoded.)
                            0xF1  SRAM write         (the 13,312-call write loop)
                            0xF3  status poll        (the 87,352 tight-loop polls)
                            0xF4  ?  (write-loop init)
                            0xF5  ?  (write-loop init)
                            0xA8  reboot             (per librtburn, but unconfirmed
                                                      on this code path)
   2     1    subcmd byte   command-specific
   3     1    arg byte      command-specific (often a flag bit / counter)
   4     1    pad           zero
   5-6   2    vendor sig    0xDA 0x0B  (RealTek vendor ID 0x0BDA, little-endian)
                            Acts as a magic sanity check for the device firmware.
   7     1    pad           zero (possibly second flags byte)
   8+    184  payload       command-specific data (firmware bytes for 0xF1, etc.)
```

**Worked example — frame 70866** (the `enable_vdcmd` call):
```
captured: 40 02 01 00 da 0b 00 00  00 00 ... (zeros to 192)
          │  │  │  │  └──┘           
          │  │  │  │   └─ vendor sig 0x0BDA (RealTek)
          │  │  │  └──── pad
          │  │  └─────── flags = 1 (one of the two bool args)
          │  └────────── opcode 0x02 = enable_vdcmd
          └───────────── direction 0x40 = WRITE
```

Disassembly produced exactly this layout:
```c
buf[0..1] = 0x4000;          // little-endian: buf[0]=0x00 (report ID), buf[1]=0x40
buf[2]    = 0x02;            // opcode = enable_vdcmd
buf[3]    = (a + 2*b);       // packed bool flags
buf[5..6] = 0x0BDA;          // RealTek vendor sig (LE: DA 0B)
// then send_vendor_cmd(buf, "enable_vdcmd")  ← debug name string
```

The command name "enable_vdcmd" is even passed alongside the buffer to a
helper that logs it on errors — confirming this is a *named* protocol
operation, not just opaque bytes.

**Worked example — frame 435330** (the per-block flash write):
```
captured: 40 f1 80 7d  00 00 80 00  [184 bytes of firmware data]
          │  │  │  │   pad      
          │  │  │  └───── low address byte (block index, sweeps 0x00–0xFF)
          │  │  └──────── high flag (0x80 = upper half of block,
          │  │                       0x00 = lower half — pair per block)
          │  └─────────── opcode 0xF1 = SRAM_write
          └────────────── direction 0x40 = WRITE
```

The vendor-sig bytes at offsets 5-6 are zeros here, suggesting that the
sig-check is enable_vdcmd-specific and not present on every command.
Confirming this is a follow-up.

**Worked example — I²C tunnel WRITE (frames 7710 / 8028 / 8154 / …):**

Decoded directly from the pcap (every `40 c6 …` frame in phase 1 has
this layout; the data tail is the only varying part):
```
captured: 40 c6 00 00 00 00 07 00  6e 00 00 00 00 00 00 00
          │  │              │      │
          │  │              │      └─ I²C target (8-bit address, here 0x6E
          │  │              │         = DDC/CI display address). Other
          │  │              │         observed targets: 0x94 (FL5500 alt)
          │  │              └─ I²C transaction length (here 7 bytes follow)
          │  └─ opcode 0xC6 = I²C tunnel WRITE
          └─ direction 0x40 = WRITE
          ... (zeros to offset 64) ...
offset 64: 51 84 c0 99 ee 20 2c    [I²C payload, `len` bytes]
           │  │  └─────────┘  │
           │  │     │         └─ DDC/CI checksum (XOR from 0x6E onward)
           │  │     └─ 4-byte DDC/CI command body
           │  └─ DDC/CI message length (0x80 | data_count)
           └─ DDC/CI source address (host = 0x51)
```

Full byte map of the 192-byte payload for `0xC6` and (verified
identical) `0xD6`:
```
offset  size  field         meaning
   0     1    direction     0x40 = WRITE  (0xC0 = READ — but I²C tunnel
                            uses 0x40 even for the READ-side opcode 0xD6;
                            the kernel pulls the response via HIDIOCGINPUT
                            after the request, not via a 0xC0 framing)
   1     1    opcode        0xC6 = I²C write, 0xD6 = I²C read/poll
   2-5   4    pad           all zero in observed frames
   6     1    i2c_len       number of payload bytes the device should put
                            on the I²C bus (write opcode) or read from it
                            (read opcode). Observed values: 7 for DDC/CI
                            requests, 0x40 (=64) for the matching response
                            puller, 2 for the closing reboot command.
   7     1    pad           zero
   8     1    i2c_target    8-bit I²C address. 0x6E = DDC/CI display.
                            0x94 appears in the final reboot frame
                            (frame 875350: `40 c6 … 02 00 94 00 01`).
   9     1    pad           zero
  10     1    bus_speed     0x00 = default. Higher values not observed in
                            phase 1; possibly unused on this part.
  11-63  53   pad           all zero
  64+    128  payload       the actual I²C bytes (`i2c_len` of them).
                            For DDC/CI traffic this starts with 0x51
                            (host source addr) and ends with the DDC/CI
                            checksum byte.
```

This layout is what the plugin's `fu_dell_monitor_rt_device_i2c_write`
and `…_i2c_read` already encode. The wire format is correct; what's
missing is the 0xE1 auth handshake that *precedes* every I²C tunnel
exchange — see "I²C tunnel auth handshake" below.

**I²C tunnel auth handshake (opcode 0xE1) — currently blocking us:**

In every observed pcap cycle, the host sends two `0xE1` frames *between*
`enable_high_clock` and the first `0xC6`:

```
40 e1 01 01 00 00 00 00  …   request — payload all zero
40 e1 03 00 00 00 00 00  … (offset 64) <8 bytes that change every cycle>
```

The 8-byte tail in the second frame differs every cycle (samples seen:
`7b 06 b6 01 df 03 e0 75`, `e1 7d b9 0e 17 4e 67 c5`, `dd 24 96 20 78 6d
b2 01`, `d9 0f 23 af a5 0a 62 22`, …). The device evidently issues a
fresh challenge each cycle and the host must compute a response. Without
this handshake the kernel STALLs subsequent `0xC6` writes (we tested
this in the plugin: `wrote -1 of 193`).

**Implication.** Before *any* I²C-tunneled work — scaler version read,
flash erase, flash write, status poll on the FL5500 — the plugin must:

1. Send `40 e1 01 01 …` (request a challenge).
2. Read back the device's 16-byte challenge via `HIDIOCGINPUT`. (The
   pcap shows the host issuing two GET_REPORTs back-to-back: the first
   returns *stale* data left over from the previous I²C-tunnel reply;
   the second returns the actual fresh challenge. The cleanest way to
   handle this in the plugin is to issue a single GET_REPORT *immediately
   after* the `40 e1 01 01` SET_REPORT and trust that the device has
   already latched the challenge by then.)
3. Compute the 8-byte response with `cal_auth` (decoded below).
4. Send `40 e1 03 00 …` with the 8-byte response at offset 64.
5. Then issue the desired `0xC6` / `0xD6` traffic.

**`cal_auth` decoded** (from `RTS5409S_HID::cal_auth` in
`libdevices.so` at `0xb8640`). Pure C, no crypto library needed:

```c
/* challenge: 16 bytes from the device. response: 8 bytes back to it. */
void cal_auth(const uint8_t challenge[16], uint8_t response[8]) {
    uint16_t mix = ((uint16_t)challenge[0] << 8) | challenge[15];
    int parity_even = !(__builtin_popcount(mix) & 1);
    uint8_t tmp[8];
    if (parity_even) {
        memcpy(tmp, &challenge[8], 8);
        unsigned idx = challenge[6] & 7;
        tmp[idx] ^= challenge[idx];          /* mix from low half */
    } else {
        memcpy(tmp, &challenge[0], 8);
        unsigned idx = challenge[14] & 7;
        tmp[idx] ^= challenge[idx + 8];      /* mix from high half */
    }
    for (int i = 0; i < 8; i++) response[i] = tmp[i] ^ key[i];
}
```

The 8-byte `key[]` is derived at device-open time by
`RTS5409S_HID::get_synkey()` from a buffer at object offset `0x18`,
which is populated by the application before `open()` is called.

**`get_synkey` decoded** (from `RTS5409S_HID::get_synkey()` in
`libdevices.so` at `0xb8570`). The function walks a per-product seed
buffer and produces the 8-byte key. Three actions per byte, gated on
bits 0 and 4 of the byte:

```c
/* seed: per-product blob.  out: 8-byte cal_auth key. */
void get_synkey(const uint8_t *seed, size_t seed_len, uint8_t out[8]) {
    size_t in = 0, op = 0;
    memset(out, 0, 8);
    while (in < seed_len && op < 8) {
        uint8_t b = seed[in], bits = b & 0x11;
        if (bits == 0x01 || bits == 0x10) {           /* FAST */
            if (in + 1 >= seed_len) break;
            out[op++] = b ^ seed[in + 1];
            in += 2;
        } else if (bits == 0x11) {                    /* SLOW */
            if (in + 2 >= seed_len) break;
            if (op     < 8) out[op]     = b              ^ seed[in + 1];
            if (op + 1 < 8) out[op + 1] = seed[in + 2]   ^ seed[in + 1];
            op += 2;
            in += 3;
        } else {                                      /* SKIP */
            in += 1;
        }
    }
}
```

The seed buffer flows from the application to `RTS5409S_HID`'s vector
through a chain of generic plugin calls in libdevices/libhub:

```
main "Firmware Updater" binary
  reads ./cert.dat (an 18-byte per-product blob, despite the name)
  ↓
  libdevices.so::load(data, len)        // libdevices's plugin entry point
  ↓
  IIC_INTF::load(vector<uint8_t>)       // forwards to inner HID
  ↓
  HID_INTF::load(vector<uint8_t>)       // assigns to vector at this+0x18
  ↓
  next call to RTS5409S_HID::open(...)
  ↓
  get_synkey() runs on the now-populated buffer → key at this+0x21A
  ↓
  hub_force_handshake() uses key in cal_auth
```

**For the U4025QW**, `cert.dat` contains exactly:
```
F8 B7 FD 21 E0 32 22 B8  A9 E8 7C 11 04 94 E2 9D  9F 6A
```
which `get_synkey` reduces to:
```c
static const uint8_t U4025QW_HUB_KEY[8] = {
    0x4F, 0xDC, 0xC1, 0x10, 0x11, 0x6D, 0x76, 0x02,
};
```

Verified two ways: (a) recovered empirically by XOR'ing the algorithm's
unkeyed output against actual responses in 4 captured handshake cycles
(frames 7704, 7996, 8122, 8148), and (b) reproduced from `cert.dat` by
running our reimplementation of `get_synkey` over the file. The two
agree byte-for-byte.

**For other Dell monitors**, just swap the `cert.dat` blob — same
algorithm. The eventual right home for the seed is inside the LVFS
firmware payload (alongside the `.upg` for the same monitor) so that
it ships with each update and we don't need to track per-model blobs
in plugin source.

#### Implications for the plugin code

Three concrete primitives are sufficient to drive the entire flash:

```c
// Wire-level: send a vendor command (any opcode), get response.
fu_dell_monitor_rt_vcmd(FuDevice *dev, guint8 opcode,
                        guint8 sub, guint8 arg, const guint8 *data,
                        gsize data_len, GError **err);

// Per-opcode helpers wrap fu_dell_monitor_rt_vcmd:
fu_dell_monitor_rt_enable_vdcmd(FuDevice *dev, gboolean a, gboolean b);
fu_dell_monitor_rt_sram_write_chunk(FuDevice *dev, guint8 block_lo,
                                    gboolean upper_half,
                                    const guint8 *chunk, gsize chunk_len);
fu_dell_monitor_rt_status_poll(FuDevice *dev, guint8 *status_out);
fu_dell_monitor_rt_erase(FuDevice *dev, guint8 erase_subcmd);
```

The existing `realtek-mst` plugin in fwupd already has the SRAM-staged
write loop and erase polling at exactly this level of abstraction; the
work is largely about plugging this transport in beneath that loop.

#### Canonical update sequence (recovered from disassembly)

Walking `FL5500_IIC_ISP::update_hub()` and the API methods underneath
gives us the full algorithmic description of one component's flash:

```c
// FL5500_IIC_ISP::update_hub() — top-level for a single component
void update_hub() {
    if (image_size != 0x10000) return error;             // expects 64 KB image
    set_sram_page();                                      // chip-mode setup
    set_spi_flash_start_addr(0x10000);                    // SPI base offset
    for (i = 0; i < 16; i++) {                            // 16 × 4 KB = 64 KB
        // status / progress reporting via ModuleMessage
        sram_to_spi_flash(
            spi_addr   = 0x10000 + (i << 12),             // 4 KB stride
            buf        = source + (i * 0x1000),
            sram_count = 0x32,                            // 50 (purpose TBD)
            retries    = 0x64                             // 100 retries
        );
    }
}

// FL5500_IIC_API::sram_to_spi_flash() — one 4KB sector
void sram_to_spi_flash(uint32 spi_addr, uint8* buf, int count, int retries) {
    write_byte(0x5009, 0x00);                             // clear control regs
    write_byte(0x500A, 0x00);
    write_byte(0x500B, 0x00);
    write_byte(0x5001, 0xAA);                             // arm trigger (magic)
    write_byte(0x5004, spi_addr & 0xFF);                  // SPI addr byte 0
    write_byte(0x5005, (spi_addr >> 8) & 0xFF);           // SPI addr byte 1
    write_byte(0x5006, (spi_addr >> 16) & 0xFF);          // SPI addr byte 2
    write_spi_sector(buf, 0x1000);                        // 4 KB
}

// FL5500_IIC_API::write_spi_sector() — stage 4 KB through SRAM, then commit
void write_spi_sector(uint8* buf, int len) {
    write_byte(0x5007, 0x00);                             // SPI control = idle
    write_byte(0x5008, 0xE0);                             // enable SPI master
    write_burst(0x6000, buf, len, chunk_size = 16);       // staged via SRAM @ 0x6000
    write_byte(0x5826, 0x10);                             // GO: copy SRAM → SPI
}

// FL5500_IIC_API::write_burst() — split into 16-byte I²C transactions
void write_burst(uint16 sram_addr, uint8* buf, uint16 len, uint8 chunk_size = 16) {
    chunk_size = min(chunk_size, 16);                     // hard cap at 16
    for (off = 0; off < len; off += chunk_size) {
        IIC_INTF::write(reg = 0, addr = sram_addr + off,
                        buf = buf + off);                  // 1 wire write each
    }
}
```

**One full HUB component update therefore emits:**

| Layer | Per-call wire writes | Per-component |
|-------|---------------------:|--------------:|
| `write_byte` register configures (in `sram_to_spi_flash`) | 7 | 16 sectors × 7 = 112 |
| `write_burst` chunks (in `write_spi_sector`, 4 KB ÷ 16 B) | 256 | 16 × 256 = 4096 |
| Trigger writes (`0x5826 = 0x10`, etc.) | 2 | 16 × 2 = 32 |
| **Total per HUB component** |  | **≈ 4 240 wire writes** |

The captured pcap showed 13,312 `40 f1 …` writes total, which fits if
the flow runs over ~3 components (HUB + DISPLAY + DISPLAY, or
HUB×3) or has additional retry/verify passes.

#### Key chip-side register map (recovered)

| Register | Value | Meaning |
|----------|-------|---------|
| `0x5001` | `0xAA` | Trigger arm (magic) |
| `0x5004` | addr[7:0] | SPI flash dest addr byte 0 |
| `0x5005` | addr[15:8] | SPI flash dest addr byte 1 |
| `0x5006` | addr[23:16] | SPI flash dest addr byte 2 |
| `0x5007` | `0x00` | SPI control = idle |
| `0x5008` | `0xE0` | SPI master enable |
| `0x5009` / `0x500A` / `0x500B` | `0x00` | Control state clear |
| `0x5826` | `0x10` | GO: SRAM → SPI flash copy |
| `0x6000`+ | data | SRAM staging window (4 KB capacity) |

#### IspStage enum (recovered from `set_progress`'s jump table)

5 values (jump table with `cmp $0x4`), mapped to monotonic progress milestones:

| `IspStage` | Sets `last_value` to | Plain meaning |
|----------:|---------------------:|---------------|
| 0 | reset/init | beginning of an attempt |
| 1 | computed percent | mid-flash progress driven by sector index |
| 2 | ≥ 5 % | erase complete |
| 3 | ≥ 10 % | handshake / pre-write |
| 4 | ≥ 100 % | done |

The progress is rendered to the user via `ModuleMessage::send` /
`ModuleMessage::update` — we'll equivalently drive
`fu_progress_set_percentage()`.

Format strings used by the message system (lifted verbatim — useful
for matching error codes against future failures and for the plugin's
own log lines):

```
Updating   => stage: [%d] - Attempt %d
Updating   => stage: [%d] - success.
Updating   => stage: [%d] - failed (%02X). Retrying...
Updating   => clear address fail. (0x%08X)
Updating   => write flash fail. (0x%08X)
Updating   => verify fw fail. (0x%08X)
Updating   => Hub Soft-Reset and wait HUB re-enumeration. (0x%02X)
Updating   => Update hub info fail. (err: 0x%02X)
Updating   => same version, skip it. (0x%02X)
Updating   => Completed.
```

The "Hub Soft-Reset and wait HUB re-enumeration" message confirms the
plugin needs to handle a deliberate hub reset after the write phase
(this matches the `addr 31 → addr 0/1` re-enumeration we observed at
~905 s in the pcap timeline).

#### Open follow-ups for this section

- [ ] Walk the `Rts5409s_IIC_ISP::isp()` (a sibling of FL5500's) to
      see the *hub-MCU* update sequence. Almost certainly very similar
      register/write structure but acting on the RTS5409S's own flash.
- [x] Decode the `0xC0 F3` poll response to understand the success-
      vs-busy bit pattern. **Done** — see "0xC0 F3 status poll" below.
- [ ] Identify the bootloader-enter command (`04 08 02 00 01`) — it
      doesn't follow the `0x40` direction format and is sent to the
      transient address 17, suggesting an early-init code path
      probably in `RTS5409S_HID::open()` rather than the ISP class.
- [ ] Map the `.upg` file's component-section payloads into the
      sequence: which 6 components correspond to which `update_*()`
      function calls, in what order.
- [ ] Confirm the assumption that the 13,312 captured `0x40 F1` writes
      represent multiple-component flashes (not a single 64 KB hub
      write × ~22 chunk-size-mismatch).

### Per-phase timeline of the captured update

Source: `/agents/ada/projects/dell-u4025qw-fw/captures/u4025qw-m3t105-update-171436.pcapng`
(17 min 57 s wall time, 901,308 frames; 444,560 bus-3 control transfers).

#### Top-level structure

| # | Phase | Frame range | Wall time | Active addr | Headline opcodes |
|---|-------|-------------|-----------|-------------|------------------|
| 1 | Discovery / version read | 7691 – 14466 | 23.6 s – 60.8 s | 13 | `40 02 01 00`, `40 06 01 00`, `40 e1 01 01`, `40 e1 03 00`, `40 c6 00 00`, `40 d6 00 00`, `40 06 00 00` |
| 2 | Idle wait (user looks at GUI) | (none) | 60.8 s – 174.6 s | — | **114-second silence** — no bus-3 control traffic at all |
| 3 | Pre-update setup | 22049 – 45826 | 174.6 s – 215.8 s | 13, 14 (briefly) | `40 d6 80 00`, `40 d6 0f 00`, `40 d6 06 00`, `c0 09 00 00`, `40 d6 2d 00`, `40 d6 f5 00` |
| 4 | **Bootloader enter** | 45830 | 216.2 s | 17 (one-shot) | `04 08 02 00 01` — single opcode, different cmd-class (`0x04` not `0x40`) |
| 5 | Re-enumeration burst | 47555 – 70102 | 219.5 s – 250.6 s | 0 → 18 → 28 → 29 → 31 → 32 | Address-0 setups; many devices drop and re-enumerate; settles to the bootloader-mode HID interface at addr 31 |
| 6 | Header read-back / inventory | 53240 – 65741 | 226.9 s – 243.4 s | 31 | 1,536 `40 c8 …` reads = 3 passes × 256 addresses × 2 flag bits (`00`/`80`) |
| 7 | **Erase wait** | 72484 – 370798 | 258.1 s – 678.1 s | 31 | 1,840 `40 d6 08 00` + 1,832 `40 d6 09 00` progress polls (~230 ms cadence) — **~7 minutes** of flash erase |
| 8 | **Write loop** | 371906 – 861324 | 680.0 s – 896.7 s | 31 | 13,312 `40 f1 <flag> <byte>` writes + 87,352 `c0 f3 00 00` status polls (~2 ms tight loop) + 4,196 block commands (`40 d6 60 00`, `40 d6 6f 00`) |
| 9 | Verify / cleanup | 861324 – 875350 | 896.7 s – 904.7 s | 31 | Trailing `40 d6 …` and `40 c8 …`; final command is `40 c6 00 00 00 00 02 00 94 00 01` at 904.7 s — likely "image complete, reboot" |
| 10 | Boot-out re-enumeration | 875354 – 883676 | 904.7 s – 940.5 s | 0 → 1 (bus reset path) | Hub-level re-enumeration as the device reboots back into normal mode |
| 11 | Final settle | 883676 – end | 940.5 s – 1077 s | 1 + others | Devices come back at fresh addresses; closing version-read presumably happens here |

#### Implications for the plugin state machine

1. **Bootloader entry is a single opcode** (`04 08 02 00 01`) sent to the
   pre-bootloader interface, not the persistent `0x40 …` interface. After
   this command the host has to wait for a re-enumeration (~3–4 seconds
   based on the gap from frame 45832 to 47555) and then rediscover the
   device at its new bus address. fwupd's `FuUsbDevice` already supports
   "wait for replug" via `fu_device_set_remove_delay()`.

2. **The erase phase blocks for ~7 minutes** with low-rate (~230 ms)
   polling. The plugin must:
   - Not assume erase is fast.
   - Surface progress via the polled status response (the `40 d6 08`/`09`
     responses presumably encode an "erase percent done" — TBD).
   - Set `fu_progress_set_steps()` so 70 % of the perceived progress is
     allocated to erase.

3. **The write phase polls *every 2 ms*** (`c0 f3 00 00`) — that's 500
   polls/s. The C plugin should match this cadence; longer intervals
   risk the device buffer filling and stalling. Easy enough with
   `g_usleep(2000)`.

4. **Re-enumeration count is at least two** — once on entry, once on
   exit. Address jumps from 13 → 31 (entry) and from 31 → 1+ (exit).
   The exit path is the one most likely to time out if the plugin
   gives up too early after sending the reboot opcode.

5. **The reboot/done command is `40 c6 00 00 00 00 02 00 94 00 01`**
   (or close to it — single instance at frame 875350). The trailing
   `94 01` might encode the new firmware version it's reporting; need
   to confirm.

#### Polling cadences observed

- `40 d6 08 00` (erase progress): 1,840 calls over 420 s → mean cadence
  **228 ms**.
- `40 d6 6f 00` (write block status): 1,607 calls over 217 s → mean cadence
  **139 ms**.
- `c0 f3 00 00` (per-write status): 87,352 calls over 215 s → mean cadence
  **~2 ms** (tight host-side spin).

Inter-arrival of consecutive `c0 f3` polls during the write phase
(first 10): 0.5257 s (just after start), then 2.3, 2.2, 2.2, 2.3, 2.2,
1.8, 1.8, 1.7, 1.8 ms — i.e., the host spins after the first response.

#### Open follow-ups for this section

- [ ] Decode the response payload of `c0 f3` to confirm it's a percent-
      done counter.
- [ ] Understand why the read-back phase iterates 3 times over each
      (address, flag) pair. Hypotheses: triple-read for ECC voting,
      or read + checksum + meta.
- [ ] Confirm that `40 c6 00 00 …` at frame 875350 is in fact the
      "reboot to new firmware" trigger (not just a status read with
      the new version embedded in args).
- [ ] Identify the "preflight failed" branches — opcodes the device
      uses to signal "wrong panel ODM" / "rollback blocked" so the
      plugin can map them to clean error strings.
- [ ] Understand the role of the pre-bootloader address-17 interface
      (the `04 08` recipient). Is this a separate USB function on the
      same physical device? An IAD-grouped sibling?

### `.upg` format — parser spec

Walked the file structurally; here is the binding spec the plugin's
`FuFirmware` subclass needs to implement.

#### Top-level layout

All length-prefixed strings are `u32 BE length` followed by bytes.

```
offset       size           field
─────────────────────────────────────────────────────────
0x000000     4              magic length = 3
0x000004     3              "UPG"
0x000007     4              format version length = 5
0x00000B     5              "1.0.6"
0x000010     4              product length = 7
0x000014     7              "U4025QW"
0x00001B     4              firmware version length = 6
0x00001F     6              "M3T105"
0x000025     4              component count   (= 6 for U4025QW LGD)
0x000029     —              component-name table (6 entries):
                              u32 BE length + ASCII name
                              ─────────────
                              len=4 "HUB1"
                              len=4 "HUB2"
                              len=4 "HUB4"
                              len=3 "HUB"
                              len=3 "PDC"
                              len=7 "DISPLAY"
0x00005A     —              per-component sections begin (see below)
                            … binary payloads + signatures …
last 96 B    96             trailing 96-byte base64-url-safe signature
                            (file-level — likely covers the whole image)
```

#### Per-component section structure (preliminary, needs runtime verification)

Right after the component-name table, each component contributes a
section that contains:

- A small leader (`u32 = 1` followed by one zero byte appears at the
  very start of the section block — purpose unknown, possibly section
  count or spec version)
- A length-prefixed component name re-emitted (re-binds payload to
  component identity)
- A small `u32` (commonly 7 in this file) — looks like a "subrecord
  count" for what follows
- One or more 96-byte base64-url-safe signature blobs (each preceded
  by `u32 = 0x60`); these are very likely raw-encoded SHA-384 ECDSA
  signatures, one per signed sub-image
- The binary firmware payload bytes for the component

Note the file's payload bytes (~1.1 MB total) are interleaved with
these per-component metadata records, not contiguous at the end.
A complete byte-by-byte map per component is the next investigation
step.

#### Component → plugin → ISP-class dispatch

Every plugin `.so` exports the same set of "module interface" symbols
(`register_module`, `load`, `start`, `stop`, `set_chunk_size`,
`get_fw_version`, `set_message_callback`, …). The main `Firmware
Updater` binary owns the dispatcher: it iterates the .upg's component
names and routes each one to the matching plugin instance, calling
`plugin->load(component_bytes)` then `plugin->start()`.

Mapping from .upg component names to plugin/ISP class:

| .upg name | Plugin .so | Concrete ISP class |
|-----------|-----------|--------------------|
| `HUB`     | `libhub.so` | `FL5500_IIC_ISP` (the scaler MCU's flash) |
| `HUB1`, `HUB2`, `HUB4` | `libhub.so` | `Rts5409s_IIC_ISP` (sub-flashes of the upstream hub MCU) |
| `PDC`     | `libpdc.so` | `RTS545X_ISP` (USB-PD controller) |
| `DISPLAY` | `libdisplay.so` | `RealtekISP` (panel scaler / TCON) |
| `BRIDGE`  | `libbridge.so` | `RTD2176_ISP` |
| `WEBCAM`  | `libwebcam.so` | `SigmaStar_Camera_ISP` (Dell C-series webcam attachment) |
| `AUDIO`   | `libaudio.so` | `ALC4058_ALC5576_ISP` / `CX20889_ISP` |
| `TOUCH`   | `libtouch.so` | `FlatFrog_Touch_ISP` |
| `TBT`     | `libtbt.so` | `GoshenRidge_ISP` / `TitanRidge_ISP` |
| `MCU`     | `libmcu.so` | `NUC125_ISP` |

For the U4025QW (LGD panel) M3T105 .upg the active set is:
`HUB1`, `HUB2`, `HUB4`, `HUB`, `PDC`, `DISPLAY`. The minimum viable
plugin that successfully reflashes the monitor needs to handle at
least `HUB` (FL5500 scaler) and `DISPLAY` (panel scaler); the
`HUB1/2/4` records appear to be sub-flashes within the hub MCU and
likely route through the same `Rts5409s_IIC_ISP` instance with
different bank IDs. `PDC` is a separate MCU and may not be strictly
required for every update — needs runtime verification.

#### Bootloader-enter — provisional answer

The exact USB-level bootloader-enter command isn't precisely
identified yet. Two leads:

1. **`RTS5409S_HID::send_vendor_cmd(buf, name)`** with the named
   command `"enable_vdcmd"` — sends opcode `0x02` in our wire format.
   This opcode IS what we observed in the pcap at the start of the
   update sequence (frame 7700 onwards) and is the most likely
   candidate.
2. The 5-byte CLASS-DEVICE control transfer (`bmRequestType=0x20`,
   `wLength=5`, payload `04 08 02 00 01`) at frame 45830 to addr 17
   does NOT match any candidate in the binary. Likely USB-stack
   overhead (e.g., a hub-class request) rather than firmware-update
   protocol.

Working hypothesis: **`enable_vdcmd` (opcode `0x02`) IS the
bootloader-enter on the U4025QW path.** Plugin will start with this
and adjust based on runtime testing.

The file `/home/vsts/work/1/s/parade_ps5512/ps5512_vdcmd.cpp`
(the source path leaked in libdevices.so) confirms "vdcmd" =
"vendor command [protocol]" and is a per-vendor extension to USB-HID.

#### Cert / config files

- `cert.dat` — 18 bytes; the per-product seed buffer for `get_synkey`
  (see "I²C tunnel auth handshake"). Despite the name, NOT a cert.
- `cert2.dat` — 26 bytes: 4-byte `de ad be ef` magic + 22-char
  Base64URL = a 16-byte AES-128 key. Almost certainly the master key
  for decrypting `appconfig.dat`. Not yet required by our plugin.
- `appconfig.dat` — 813 bytes encrypted. App tunables (config keys
  visible in the main binary's strings near the `appconfig.dat`
  reference: `skip_version_check`, `enable_application_retry`,
  `mchp_hub_rom_code_pid`, `scan_devices_extra_delay`, …). Some
  (rollback version, scan delays, ROM-code PIDs) are quirks Dell
  relies on across monitor models. *Different decryption scheme from
  `decrypt_string` — uses a different binary format and probably a
  different key. Not yet cracked, not blocking any plugin work.*

#### Component → plugin → chip dispatch (in the main binary)

Reverse-engineered by Ghidra-decompiling the main `Firmware Updater`
binary (`/home/vsts/work/1/s/plugins/pluginmanager.cpp` plus per-slot
interface files `audio.cpp`, `bridge.cpp`, `display.cpp`,
`ethernet.cpp`, `hub.cpp`, `mcu.cpp`, `pdc.cpp`, `tbt.cpp`,
`touch.cpp`, `tts.cpp`, `webcam.cpp`):

1. **Plugin discovery** — main scans `plugins/`, calls `dlopen` on
   each `.so`, then `dlsym("register_module")` to get its module GUID.
2. **GUID → interface class** — main has a hardcoded chain of
   `string::compare` against per-slot module GUIDs. On match it
   constructs the matching interface class wrapper (sized 0x160 to
   0x1b8 bytes depending on the slot) and stashes it at a fixed
   slot-specific offset in the application context (e.g. hub at
   +0x180, pdc at +0x188, display at +0x190, ...).
3. **Interface wrapper** — each wrapper dlsym's the standard plugin
   API: `register_module`, `get_handle`, `release_handle`, `load`,
   `start`, `check_start`, `pause_device`, `reset`,
   `set_device_handler`, `set_message_callback`, `set_device_info`,
   `set_device_config`, `set_device_path`,
   `set_affiliated_device_path`, `get_string`, `get_fw_version`,
   `get_custom_info`, plus hub-specific extras (`set_other_info`,
   `set_start_index`, `set_chunk_size`, `get_model_name`,
   `get_flash_wp_status`).
4. **Component → interface routing** — the .upg's component name
   (e.g. `"HUB"`, `"DISPLAY"`) is matched to one of the interface
   wrappers via the per-slot interface .cpp logic. Each interface
   knows it serves one specific slot family.
5. **Interface → chip type → ISP class** — the interface wrapper's
   `get_handle()` ends up calling `libhub.so::get_handle("PARADE
   FL5500 IIC")` (or similar), which scans libhub's 9-entry chip-type
   LUT and constructs the matching ISP class (e.g.
   `FL5500_IIC_ISP`).

**Important correction.** The chip-type STRING ("PARADE FL5500 IIC")
that step 5 receives doesn't actually need to be decrypted at runtime
in our plugin's code path — it's a *libhub-internal* lookup key that
maps onto the chip-type GUID. Both ends of that lookup are stable
across monitor models. **What the .upg actually carries is the
chip-type GUID** (e.g. `55afe793-…` for RTS5409S IIC), encrypted with
a per-product passphrase using the `decrypt_string` scheme below.
Our plugin reads the GUID directly from the .upg, then uses our own
GUID → handler dispatch table — bypassing the chip-type-name lookup
entirely. See "Per-component metadata" below for the full picture.

#### `decrypt_string` — algorithm and passphrase format (CRACKED)

`decrypt_string()` (defined locally in each plugin .so as
`_Z14decrypt_stringPKcS0_`) is exactly the CryptoPP template
instantiation visible in the type names:

```cpp
DataDecryptorWithMAC<Rijndael, SHA256, HMAC<SHA256>,
                     DataParametersInfo<16, 16, 32, 8, 2500>>
```

That is: AES-128 + HMAC-SHA256 with 8-byte salt, 32-byte MAC key,
2500 PBKDF1 iterations using SHA-256. The function takes
`(passphrase, base64url_ciphertext)` → plaintext; the input is
Base64URL-decoded into raw bytes that CryptoPP's
`DataDecryptorWithMAC` consumes directly.

**Passphrase format** (recovered by gdb-tracing every `decrypt_string`
call at startup):

```
Wistron@<MODEL>
```

For the U4025QW the literal string is `Wistron@U4025QW`, embedded as a
const C string in main `Firmware Updater`'s `.rodata` (visible in the
trace at `ptr=0x5555558f8d50` "in Updater"). Wistron is the OEM (Dell
contracts panel + integration to them), and `<MODEL>` is the
short-form model identifier — the same string that's also present
unencrypted in the .upg header at offset `0x14`.

**Reproducing it ourselves**: `ghidra/scripts/decrypt-blob.cpp`
re-uses CryptoPP and is a 50-line tool. `decrypt-blob <passphrase>
<base64url>` → plaintext. Built with
`g++ -std=c++17 -o decrypt-blob decrypt-blob.cpp -lcryptopp` inside a
`nix-shell -p cryptopp gcc`. Already verified against multiple
captured (input, output) pairs from the gdb trace and against the
.upg's per-component metadata.

#### Per-component metadata (decrypted from the .upg)

Once we have `decrypt_string`, every Base64URL run in the .upg
decrypts cleanly under `Wistron@<MODEL>`. The script
`ghidra/scripts/dump-upg-metadata.py` walks the .upg, tries each
≥88-char Base64URL run, and prints the plaintext. For the U4025QW
M3T105 .upg this gives us, per component, fields like:

```
8× '0'              # placeholders / reserved
'U4025QW'           # product short name
'1.04'              # per-component version
'2025-10-09'        # build date
'F9FE'              # 4-char hex (likely CRC32 of the binary payload)
'55afe793-98e8-470e-ad09-993be2b3b016'   # chip-type GUID
'5f3ba3d6-a0bd-4270-9938-814a45d5c824'   # affiliated/related chip-type GUID
'0x0bda'            # USB VID
'0x1100'            # USB PID
'0xD4'              # I²C target byte (or chip-internal index)
... more zero placeholders ...
'0x800', '0x4400'   # flash size info (capacity / region size)
'753.0AK01.0007'    # panel ID (from EDID)
```

**The chip-type GUID is the dispatch key our plugin needs.** It maps
1:1 onto a chip-handler (the same way each Dell plugin .so's
`get_handle()` switch case does). For the GUIDs we've seen so far in
the U4025QW M3T105 bundle:

| GUID | Chip family | Plugin .so providing it | Our handler |
|---|---|---|---|
| `55afe793-98e8-470e-ad09-993be2b3b016` | REALTEK RTS5409S IIC | libhub | TODO |
| `5f3ba3d6-a0bd-4270-9938-814a45d5c824` | REALTEK RTS5418E USB | libhub | TODO |
| `ea72869e-aa74-401c-8eda-8cf53ab7be72` | unknown — likely a Parade scaler since it's the second GUID on the HUB component | TBD | TODO |
| `23d1218b-1805-42af-874c-1316003b6c7d` | Dell-internal model/SKU GUID | n/a | (used for matching only) |

The complete chip-type GUID table (across all of libhub.so's 9
entries, plus libpdc / libdisplay / others as we decompile them) is
the mapping our plugin source needs to encode. That table is small,
stable, and fully reverse-engineerable from the plugin .so's static
dispatch tables (which we've already seen for libhub).

**Per-product passphrase ⇒ multi-monitor support**: any Dell monitor
.upg should decrypt with `Wistron@<MODEL>` where `<MODEL>` comes from
the .upg's product field. So our plugin works for any Dell monitor
shipped through this same `Wistron@…` scheme without per-monitor
hardcoding — the only per-monitor knowledge is the chip-type GUIDs,
which the .upg itself supplies.

**Solved.** A gdb-trace of `decrypt_string` at startup revealed the
passphrase format (`Wistron@<MODEL>`) and confirmed the algorithm
matches the CryptoPP type names. From there we wrote a small
CryptoPP-using `decrypt-blob` tool and used it to dump every
encrypted blob in the .upg in plaintext. The .upg's per-component
metadata explicitly carries the **chip-type GUID** for each
component, which is all our plugin needs to dispatch. See
"`decrypt_string` — algorithm and passphrase format (CRACKED)" and
"Per-component metadata" below for the full story.

#### `Certify::d` — binary firmware payload encryption (CRACKED)

The actual firmware binaries inside each .upg component (the bytes
that get pushed into a plugin's `load(self, vector<uint8_t>)`) are
*also* encrypted, but with a completely different scheme from the
metadata. `libhub.so::Certify::d` (offset `0x80990`) is the path
that runs on every blob:

1. **ECIES** decrypt with a static secp521r1 private key.
2. **Gunzip** the decrypted plaintext.
3. **Hex-decode** the inflated text into the final firmware binary.

The ECIES suite parameters were lifted directly from the decompiled
function (the CryptoPP type names show up explicitly):

```cpp
typedef CryptoPP::ECIES<
    CryptoPP::ECP,                             // curve over prime field
    CryptoPP::SHA3_512,                        // hash for KDF + MAC
    CryptoPP::IncompatibleCofactorMultiplication, // cofactor=2
    /*DHAES_MODE=*/true,
    /*LABEL_OCTETS=*/false> DellECIES;
```

The curve OID encoded in the embedded private key is `1.3.132.0.35`
(secp521r1). KDF = `P1363_KDF2<SHA3_512>`, MAC =
`HMAC<SHA3_512>`. Output of `Certify::d` is the firmware binary
followed by a 222-byte trailer that `Certify::c` validates:

| Bytes | Meaning |
|---|---|
| 0…131  | Raw ECDSA signature (`r ‖ s`, IEEE-1363, 66 bytes each) |
| 132…221 | DER `SubjectPublicKeyInfo` carrying a *compressed* secp521r1 signing pubkey |

Signature alg is `ECDSA-secp521r1-with-SHA3_512` (also visible as
`DL_VerifierBase<ECPPoint,…,SHA3_512,…>` in libhub.so's typeinfo).

##### Where the key lives

The PKCS#8-encoded EC private key is a literal byte array in
`libhub.so`'s `.rodata` at symbol `_ZL13CK_PV_RawData` (offset
`0x19ace0`, 98 bytes). For the U4025QW M3T105 build:

```
30 60 02 01 00 30 10 06 07 2a 86 48 ce 3d 02 01
06 05 2b 81 04 00 23 04 49 30 47 02 01 01 04 42
01 74 26 f1 02 5d d6 6a 3d f6 a1 e1 da 1d 9d d7
ae d4 ca 61 86 f2 6e ee 83 30 71 7b 81 85 58 55
2f ea e2 3b 18 a7 1d 3e d9 29 74 a3 ec 5d 5b 4f
c4 c8 a0 53 63 cd 38 b2 80 b5 66 e5 f0 e3 fc de
ce 7a
```

Saved verbatim as `captures/CK_PV.pkcs8.der` and as PEM in
`captures/CK_PV.pem`.

##### Reproducing it ourselves

`ghidra/scripts/ecies-decrypt.cpp` is a 100-line Crypto++ program
that takes the PKCS#8 key + an encrypted blob and runs the full
ECIES → Gunzip → HexDecode pipeline. Verified end-to-end against
the live capture: a 71,848-byte encrypted slice from libhub's
input vector decrypts to 131,072 bytes of firmware (byte-identical
to the plaintext we captured at the per-plugin `load()` call) plus
a 222-byte signature/pubkey trailer.

```bash
nix-shell -p cryptopp gcc --run \
  'g++ -std=c++17 -O0 -o ecies-decrypt ecies-decrypt.cpp -lcryptopp'
./ecies-decrypt CK_PV.pkcs8.der encrypted-slice.bin out.bin
# → out.bin = firmware (131072 bytes) + signature trailer (222 bytes)
```

##### Implications for the plugin

The private key is shipped in every customer's libhub.so, so the
ECIES layer is **obfuscation, not security** — anyone with the .deb
can decrypt. The integrity guarantee comes from the ECDSA
signature trailer (which the plugin can re-verify if we extract
the signing root pubkey, but isn't strictly required for
flashing).

Practical options for the fwupd plugin:

1. Hardcode the U4025QW key (and any other known-model keys) in
   the plugin source as a small key table.
2. Ship the key as a small data file alongside the plugin.
3. Build a one-shot extractor (`extract-key /path/to/libhub.so`)
   that pulls `CK_PV_RawData` from any libhub.so the user provides.

**The key is fleet-wide and time-stable** (empirically verified
against two unrelated bundles):

| Bundle | Released | libhub.so build | `CK_PV` SHA-256 |
|---|---|---|---|
| `Dell_U4025QW_FWUpdate_M3T105_Ubuntu.deb` | 2024 monitor | Oct 9, 2025 | `0eede35b…892c7bd0` |
| `Dell_U3224KB_FWUpdate_M2T105_Ubuntu.deb` | 2023 monitor | Dec 12, 2023 | `0eede35b…892c7bd0` |

Same 98-byte PKCS#8 blob, two completely different monitors,
~2 years apart in build dates. The U3224KB binary is stripped
(no `_ZL13CK_PV_RawData` symbol), but the literal bytes are still
locatable by searching for the PKCS#8 secp521r1 wrapper prefix:

```
30 60 02 01 00 30 10 06 07 2a 86 48 ce 3d 02 01
06 05 2b 81 04 00 23 04 49 30 47 02 01 01 04 42
```

This is a fixed-format ASN.1 wrapper (anything matching is a
98-byte PKCS#8 ECPrivateKey on `secp521r1`), so the same grep
should pick the key out of any future Dell-monitor `libhub.so`
the Wistron stack ships.

**Plugin implication: hardcode the key.** It looks like Wistron's
signing/encrypting infrastructure produces this single keypair
for every Dell monitor it builds, so a single 98-byte constant
in our plugin source covers the whole supported fleet. If a
future bundle ships with a different key, swap the constant.

#### Open follow-ups for this section

- [ ] Walk one component's bytes end-to-end to lock down the
      per-section layout (HUB1 from offset 0x5A, since it's the
      first/smallest).
- [ ] Confirm signature placement and key (Crypto++ ECDSA P-384 is
      almost certainly the algorithm; `cert.dat`/`cert2.dat` may
      be the public key fingerprint).
- [ ] Determine the order in which the dispatcher processes
      components — left-to-right by name table order, or grouped
      (HUB-family first, then PDC, then DISPLAY)?
- [ ] Verify that omitting `PDC` or `DISPLAY` is safe (so the
      plugin can ship with HUB-only support initially and add
      others as separate child devices).

### Device addressing during update

> The HID interfaces re-enumerate several times during the flash (we
> observed addresses 13, 14 → 17, 18 → 31 etc.). Need to know whether
> a fwupd plugin needs to track the "same" logical device across these
> address changes (probably yes — `FuUsbDevice` has an
> `incorporate()` mechanism for this).

- [ ] Number and timing of bootloader-mode re-enumerations.
- [ ] Whether the device descriptor changes (different bcdDevice /
      product string) between modes. If yes, that's a discrimination
      point for "is this device in bootloader mode?".

### Multi-monitor / multi-panel-ODM coverage

- [ ] Confirm the same opcode set works on the AUO/BOE/CSOT panel
      variants of the U4025QW (someone with one of those would need
      to capture).
- [ ] List of other Dell monitor models that ship with the same Wistron
      updater architecture (the `.deb`'s udev rules cover dozens of
      USB IDs hinting at a wider monitor lineup).
- [ ] Per-model GUID / ID strategy for LVFS once Dell publishes
      payloads upstream.

### Safety + UX

- [ ] Confirm the protocol's behavior on aborted updates (device left
      in bootloader, or recovers automatically?). Determines whether
      `attach()` needs a recovery path.
- [ ] What's the retry behavior of `script_EraseStatusCheck` — is the
      poll cadence fixed in firmware or driven by the host?
- [ ] How does the protocol surface "wrong panel ODM"? We hit
      *"This monitor may manufactured by other ODM"* in Dell's GUI;
      need to know which opcode the check rides on so the plugin
      can refuse the wrong `.upg` early.
- [ ] Downgrade handling. Strings include `minimum_rollback_version`
      and `BLOCK_SAME_UPG_PROJECT_VERSION` — find the on-device check
      and surface it correctly to fwupd's `flags` machinery.

### LVFS / Dell engagement

- [ ] Reach out to Dell Linux Engineering. Existing relationship for
      `dell-dock` makes this a relatively short conversation.
- [ ] Decide whether to upstream into `fwupd` proper or keep as an
      out-of-tree plugin distributed separately.

---

## Version semantics + panel binding (decoded 2026-05-06)

There are **three distinct kinds of "version"** in this protocol, and
conflating them will produce subtly wrong skip-or-update decisions.
Spelling each out before any verify-before-flash code lands.

### 1. Chip persistent firmware version (`opcode 0x09`, "get_fw_version")

`R 0xC0 0x09` reads back a struct from each USB-connected hub MCU. The
relevant fields:

```
offset 0..1   00 0b           reserved
offset 2..3   da 04           RealTek vendor sig
offset 4..6   83 54 83        chip-class header (constant)
offset 7      81 / 82         chip-class id (0x81 primary, 0x82 secondary)
offset 8..10  81 ff 02        constant
offset 11..13 03 04 02        firmware version triplet (major.minor.patch)
offset 14..16 03 40 03        constant
…             zeros / padding
```

Captured values for this monitor pre-update:
- Primary (DEV_1100, RTS5409s): chip-class `0x81`, fw `3.4.2`
- Secondary (DEV_1101, RTS5418E): chip-class `0x82`, fw `4.6.2`

Dell's binary calls `0x09` exactly twice per chip — once before the
install for diagnostics, once after to confirm the new fw landed. It
does **not** drive the skip-or-update decision.

### 2. Package state register (DDC/CI VCP `0xAD`, the "IspTag")

`51 84 c0 99 ad 18 57` to slave `0x6E` reads the chip's **`IspTag`**
register. The reply embeds the installed-package version between '#'
delimiters with a 3-letter state prefix:

- `#ISP#M3T105#`
- `#CHK#M3T105#`

**Read wire format** (DDC/CI selector `0xAD`, sub `0x18`):

```
host → 6e:  51 84 c0 99 ad 18 57           (length=4, vendor sig 0x99, sel 0xAD)
6e → host:  51 9a c1 99 23 49 53 50 23 …   (#ISP#M3T105#…)
                       ↑     ↑           ↑
                       '#'   ISP        '#' (separators)
```

**Write wire format** (same selector, different sub-opcode `0x55`):

```
host → 6e:  51 99 c0 55 ad 23 49 53 50 23 4d 33 54 31 30 35 23 00…00 e9
                       ↑   '#  I  S  P  #  M  3  T  1  0  5  #'  (zero pad) csum
                       sub-op `0x55` = WRITE
```

**Decomp evidence (Dell `Firmware Updater` binary):**

- `FUN_00317400` builds `"#ISP#" + version + "#"` and writes it via
  vtable offset `0x1a0` with key `"IspTag"`. Called near install
  start (line 346807, line 353258).
- `FUN_00317d00` is bigger: does a buffer send via vtable offset
  `0x20`, then *conditionally* (gated on
  `*(param_1+0x1b0) == 0` AND the `0x20` method returning 0) builds
  `"#CHK#" + version + "#"` and writes it via the same
  `"IspTag"` key. Called from line 349304. This is the post-verify
  commit — and it has visible code paths where CHK doesn't fire (failed
  verify, flag set).
- `postpone_isptag_command` config flag (line 123249) controls
  whether the IspTag write happens up-front or deferred.
- `skip_version_check` config flag (line 123214) can disable the
  skip-or-update decision the read drives.

**Empirical observation in our captured trace (clean install,
captured from start):**

| ev    | direction | content                                        |
|-------|-----------|------------------------------------------------|
| 206   | read      | response: `#ISP#M3T105#`                       |
| 218   | read      | response: `#ISP#M3T105#`                       |
| 230   | write     | `#ISP#M3T105#` (Dell stamps install start)     |
| 242   | write     | `#ISP#M3T105#` (Dell repeats — extra delay)    |
| 21331 | write     | `#CHK#M3T105#` (Dell commits post-verify)      |

**Live readback after the install completed** (using the standalone
`read_isptag.py` tool against the real monitor, post-reboot, no Dell
software running):

```
IspTag wire: 51 9a c1 99 23 43 48 4b 23 4d 33 54 31 30 35 23 …
IspTag ascii: Q...#CHK#M3T105#…
```

So **CHK persists across power cycles**, confirming the original
semantics: `#ISP#…` is "install marked but not yet committed";
`#CHK#…` is "post-verify committed"; both are persistent. The chip
was at `#ISP#…` at the start of our pcap because *some prior install
on this monitor hit one of the gating conditions in `FUN_00317d00`*
(verify failure, or the `0x1b0` flag set) and never reached its CHK
write. After our captured install completed Dell's verify path
correctly, the CHK write landed and survives reboots.

**Does Dell branch on the prefix?** Walked all 7 `"IspTag"`
references in `firmware-updater.c`:

| line  | function          | role                                                |
|-------|-------------------|-----------------------------------------------------|
| 82447 | FUN_00195980      | builds an `IspTag`/`DISPLAY` tag pair for a label   |
| 346767, 346785 | FUN_002fb1c0 | diagnostic read-sleep-read pattern (extra_delay-gated)  |
| 360789 | FUN_00317400      | **writes `#ISP#…` at install start** (unconditional)|
| 361533 | FUN_00317d00      | **writes `#CHK#…` at install end** (verify-gated)   |
| 362745 | info-pass         | reads IspTag alongside ServiceTag etc.; only ServiceTag is compared |
| 363134 | FUN_0031afc0      | appends "IspTag" to a fields-to-read list           |
| 364802 | FUN_0031f4b0 PreCheck | conditionally reads IspTag (gated on `postpone_isptag_command`) for part-number lookup |

**Dell's binary never branches on the `ISP#` vs `CHK#` prefix.** It
reads the value, logs it (`"IspTag = #ISP#M3T105#"`), and proceeds
with the install regardless. The skip-or-update decision is gated on
`skip_version_check` against the version field — not the prefix.

There's a config flag named `ignore_incomplete_warning_message` that
*suggests* an "incomplete" warning exists somewhere, but the
corresponding warning-emit site isn't in the code paths we've
decoded. Could be model-specific (the `[P5525QC] Ignore UPDATE_ERROR`
string nearby suggests model-keyed messages), GUI-rendered via
Sciter, or future/dead code.

**For the plugin's verify-before-flash:**

- Read the `IspTag` version field. If it equals the .upg's
  `fw_version`, skip the install (chip is already at target).
- Surface the prefix in diagnostics for humans, but don't gate plugin
  behavior on it — Dell doesn't, and our pcap evidence shows the
  chip flashes successfully starting from either state.

### 3. Per-component metadata `version` field (in the .upg)

Each component's encrypted metadata carries a `version` string:

| Component | metadata version | Meaning |
|-----------|------------------|---------|
| `753.0AK01.0007` | `M3T105` | persistent scaler firmware target |
| `DISPLAY` | `M3T105` | persistent display payload target |
| `HUB` | `3.03` | ephemeral ISP shim version (NOT chip's runtime fw) |
| `HUB1` | `1.04` | ephemeral i2c-loaded ISP shim version |
| `HUB2` | `2.04` | ephemeral i2c-loaded ISP shim version |
| `HUB4` | `4.05` | ephemeral ISP shim version (NOT chip's runtime fw) |
| `PDC` | `f807.12.e9.…` | persistent PDC firmware target |

The numeric mismatch with the chip's `0x09` runtime fw triplet
(`HUB=3.03` metadata vs `3.4.2` chip; `HUB4=4.05` vs `4.6.2`) is not a
bug — it's a category error. The metadata version on **ephemeral
components** describes the ISP shim binary's own version, not the
chip's persistent firmware version. Comparing them directly is
meaningless.

For **persistent components** (`753.0AK01.0007`, `DISPLAY`, `PDC`), the
metadata version *is* the target firmware version. Useful for
diagnostics, but the package-level skip-or-update decision is already
covered by the `IspTag` read above.

### Other DDC/CI VCP selectors Dell sends

Decoded from request/response pairing in the captured trace; all read
scaler-stored metadata, all informational:

| Selector | Returns | What it is |
|----------|---------|------------|
| `c0 99 cc 20` | `M3T105` | package version (short form, no state prefix) |
| `c0 99 ad 18` | `#ISP#M3T105#` (or `#CHK#…#`) | package version + state prefix (`IspTag`); prefix semantics not yet decoded |
| `c0 99 ee 20` | `753.0AK01.0007` | scaler firmware ID — **this is also the panel ID** (see below) |
| `c0 99 aa 14` | `VN0VV8X4WS700617297L` | Dell service tag |
| `c0 99 ab 20` | `B7LM884` | asset/SKU id |
| `c0 99 dd 20` | `U4025QW` | product model |
| `c0 01 e6 00` | `01 43` (2 bytes) | status byte |
| `01 fe 03 00` | `02 00 fe 00 ff ff …` (binary) | panel/EDID descriptor (used for the slave-`0x42` EDID read path) |

The `cc 20` and `ad 18` selectors return the same M3T105 string; `ad 18`
just wraps it with the prefix. We use `ad 18` so the prefix is
captured for diagnostics — the version field is what we actually
consume.

### Panel binding — how the .upg encodes it and how Dell looks it up

The .upg header carries two parallel string lists:

```
name_table   = ["HUB1", "HUB2", "HUB4", "HUB", "PDC", "DISPLAY"]
panel_bound  = ["DISPLAY"]
```

`name_table` declares all the component types in the bundle; the
component section then carries `name_table.size + panel_bound.size`
entries. The first 6 are keyed by the plaintext name from
`name_table` and apply universally. The 7th is keyed by the
**plaintext panel ID** the .upg targets — for our M3T105 bundle that
key is `753.0AK01.0007`, and the entry's metadata sets
`panel_bound: true, panel_id: 753.0AK01.0007`. The presence of
`"DISPLAY"` in `panel_bound` says "the DISPLAY component type is
panel-bound; pick the entry whose key matches the connected panel's
id."

So a panel-binding check is:

1. Read the panel id from the chip — the value the **scaler returns
   via VCP `0xEE 20 2c`** (the same `753.0AK01.0007` we've been
   labeling as "scaler firmware ID"). The scaler's firmware ID *is*
   the panel id from this protocol's perspective: each panel SKU
   gets its own scaler firmware build, so the firmware ID uniquely
   names the panel.
2. Look up that string in the .upg's panel-bound component keys.
3. If a match exists → use that entry's payload as the DISPLAY
   firmware for this install. If no match → refuse the install
   (the connected panel is not one this .upg supports).

This is much simpler than the slave-`0x42` panel-bus reads suggest.
Those reads (`08 04 46 4c {72,61,77} 64` to slave `0x42` —
`FLrd`/`FLad`/`FLwd`) are a separate panel-side i2c protocol that
appears to read panel registers directly (capabilities, mode, …) for
informational purposes, not for binding identification. Empirically
verified: pairing each `c6`→`0x42` write with its `d6`+ioctl response
across the captured trace shows every reply is `04 00 00 …` (a 1-byte
ack followed by zeros). The actual panel data, if any, would come
through a separate read path (the longer `09 20 …` variants) — but
none of those replies carry the `753.0AK01.0007` panel id we'd need
for binding. The binding check uses VCP `0xEE` only.

**Implications for the plugin's verify-before-flash:**

- `read_panel_id(self)` — issue VCP `0xEE 20 2c`, parse the ASCII
  reply. Returns the panel id string.
- `read_isptag(self)` — issue VCP `0xAD 18 57`, parse `{state_prefix,
  version}`. Renamed from `read_scaler_version` to be honest about
  the semantics.
- Pre-flash verify pass:
    1. Read `IspTag`. Log the prefix for diagnostics. **Don't** gate
       on it — we don't know what `ISP#` vs `CHK#` means yet.
    2. If `IspTag` version == .upg `fw_version` → already at target,
       return success without IO.
    3. Read panel id via VCP `0xEE`.
    4. For each component with `panel_bound: true`, check that the
       component's `panel_id` equals the read value. (In practice
       there is at most one such component per category — the .upg
       only contains the panel-bound entries the bundle targets, so
       a non-match means this monitor isn't supported by this .upg.)
    5. If panel mismatch → fail before any IO with a clear "this .upg
       targets panel X, monitor reports panel Y" error.

### How Dell's binary does this (from the decomp)

The relevant code in `firmware-updater.c`:

- `firmware-updater.c:360764` — concatenates `"#ISP#"` + version
  string and sends it as the `IspTag` command (writing the state).
- `firmware-updater.c:123214` — checks the `skip_version_check`
  config flag; the version comparison happens in the surrounding
  function and is gated by this flag.
- `firmware-updater.c:123249` — `postpone_isptag_command` flag
  controls whether the IspTag write happens up-front or deferred.

The chip-side handler is in `libdevices.so` —
`RTS5409S_HID::update_hub_info(PROG_CFG)` is the wrapper that
implements `IspTag` write + read. The `PROG_CFG` struct holds the
package's identification fields (decoded earlier in this doc). Worth
a deeper Ghidra pass once we wire up the verify-before-flash code,
but the wire format is what we've already decoded — the decomp is
just confirmation.

---

## Plugin status — what works today

Snapshot of the in-tree plugin at `/agents/ada/projects/fwupd/plugins/dell-monitor-rt/`.

### Working

- **Build & load.** Plugin builds against fwupd 2.0.16 inside the
  `nix develop` shell; `fwupdtool get-plugins` lists `dell_monitor_rt`
  after `fwupd-stage-quirks` has been run.
- **Device detection.** The plugin matches only the upstream hub MCU's
  HID interface (`HIDRAW\VEN_0BDA&DEV_1100`) and surfaces it as a
  single FuDevice named `U4025QW`. The monitor exposes a second HID
  interface (`DEV_1101`) which appears to belong to the dock-side
  Ethernet MCU (the U4025QW has a built-in Ethernet port — that
  controller has its own firmware and HID interface). Its update path
  is a separate problem and we don't enumerate it for now.
- **`enable_vdcmd` (opcode 0x02).** SET_REPORT with the RealTek vendor
  signature (`DA 0B`) at wire bytes 5-6 is accepted on both interfaces.
  Earlier wire-format mistakes (sig at 4-5 instead of 5-6) are fixed.
- **`enable_high_clock` (opcode 0x06 sub 0x01).** Accepted, no payload.
- **Hub MCU version read (opcode 0x09).** Sends a READ-direction frame
  (`C0 09 00 00 00 00 20 …`) and pulls the response via the
  `HIDIOCGINPUT` ioctl — *not* fwupd's `fu_hidraw_device_get_report`,
  which is misnamed and reads the interrupt-IN endpoint. Output for our
  monitor's main hub MCU: `2.04`, matching the `"%X.%02X"` format
  string in `libdevices.so`. This is the RealTek hub MCU's *own*
  firmware revision; we log it as a diagnostic and use the M3T105
  user-facing string (read separately) as the device version.

- **`cal_auth` key derived from first principles.** The plugin no
  longer hard-codes the U4025QW key; it embeds Dell's `cert.dat` blob
  verbatim and runs our `get_synkey` reimplementation over it at
  device setup, producing the same 8-byte key Dell's binary derives.
  Plumbing for swapping `cert.dat` per-monitor is trivial — the seed
  byte array is the only model-specific input.
- **I²C tunnel end-to-end.** With the derived key fed into
  the `0xE1` handshake, the plugin successfully:
  1. Sends a DDC/CI request through the `0xC6` write opcode.
  2. Sleeps 50 ms (the FL5500 needs time to compose its reply).
  3. Pulls the response via `0xD6` + `HIDIOCGINPUT`.

- **User-facing firmware version read.** The DDC/CI selector
  `c0 99 cc 20` returns the same Dell-branded firmware identifier
  the GUI displays. For our monitor that's `M3T105`. The plugin
  reads this and exposes it through `fu_device_set_version`.

  Other DDC/CI `c0 99 XX YY` selectors observed in Dell's pcap (all
  return short ASCII strings; useful as instance-IDs and serials):

  | Selector | Returns | Meaning |
  |---|---|---|
  | `c0 99 cc 20` | `M3T105` | **Firmware version** (used by plugin) |
  | `c0 99 ee 20` | `753.0AK01.0007` | Panel ID (LG `0AK` model code) |
  | `c0 99 aa 14` | `VN0VV8X4WS7006172974` | Dell service tag |
  | `c0 99 ab 20` | `B7LM884` | Board / MCU code |
  | `c0 01 e6 00` | 2-byte status | DDC/CI feature flag |

  `fwupdtool get-devices` now reports the device as
  `Current version: M3T105`.

### Notes — two HID interfaces, two MCUs

The U4025QW's USB enumeration shows two `0bda:11xx` HID interfaces on
different sub-paths of its internal hub. **They are independent MCUs,
not duplicate paths to the same chip:**

- `DEV_1100` (path `…/3-1.4.1.5`): the upstream RTS5409S hub MCU.
  Hub-version read returns `2.04`. cal_auth handshake works. I²C tunnel
  to FL5500 works. Where the M3T105 update flows. This is what we
  match in our quirk.
- `DEV_1101` (path `…/3-1.4.2.4`): the dock-side Ethernet controller.
  USB descriptor identifies it as `0bda:8156` = Realtek RTL8156
  (2.5 GbE). Hub-version read returns `2.06`. cal_auth STALLs because
  there's no second chip behind it that needs gating; it just doesn't
  implement that opcode.

In the captured update Dell's binary sent to *both* MCUs:

| Traffic to DEV_1100 (scaler path) | Traffic to DEV_1101 (Ethernet) |
|---|---|
| `40 02` enable_vdcmd | `40 02` enable_vdcmd |
| `40 06` enable_high_clock | `40 06` enable_high_clock |
| `c0 09` get_fw_version | `c0 09` get_fw_version |
| `40 e1` cal_auth handshake | — |
| `40 c6` / `40 d6` I²C tunnel | — |
| `40 f1` / `40 f3` SRAM write loop | — |
| `40 06` close | `40 06` close |

So the Ethernet MCU uses the **same vendor-command stack** we already
implement (`enable_vdcmd` → `enable_high_clock` → opcode 0x09 read).
Dell's binary only *probed* it for a version readout in this capture
and then closed without updating. The capture was a re-flash of
M3T105 onto a monitor already at M3T105, so the binary cannot have
been short-circuiting on a same-version check — meaning the M3T105
.upg simply has no Ethernet-MCU component to push. Other monitor
firmware bundles may include one.

Adding Ethernet-MCU support later means: match `DEV_1101` in the
quirk, skip the cal_auth/I²C-tunnel steps, and reuse `vcmd`,
`vcmd_read`, `read_version`. No new protocol reversing needed for
discovery; only the actual update flow (which we'd need a fresh
capture for, since this pcap doesn't include one).

### Blocked

- **Erase / write / status-poll path on the FL5500.** Wire formats
  are already documented in PLUGIN_NOTES, but the actual sequencing
  (handshake-per-write-burst, polling cadence, etc.) needs to be
  pulled from the disassembly of `FL5500_IIC_API::write_burst` and
  friends. None of this is *blocked* now that the tunnel works —
  it's just remaining work.

### Next milestone

1. **Recover the slot→plugin→chip→protocol dispatch matrix.** This is
   what makes our plugin a real Dell-monitor plugin (vs. a U4025QW
   one-off) and what justifies upstreaming. See "Component → plugin
   → chip dispatch" above for the four candidate paths.
2. Implement the bootloader-enter `04 08 02 00 01` opcode and the
   re-enumeration wait.
3. Implement `0xF1` SRAM-write loop with `0xF3` status polling — the
   actual flash-write code path. We have wire formats for both.

### Plugin file layout (current)

```
plugins/dell-monitor-rt/
├── dell-monitor-rt.quirk            quirk-based hidraw enumeration
├── fu-dell-monitor-rt-device.{h,c}  FuHidrawDevice subclass with all
│                                    primitives (vcmd, vcmd_read,
│                                    i2c_write, i2c_read,
│                                    read_version, read_scaler_version)
├── fu-dell-monitor-rt-plugin.{h,c}  registers the device gtype
└── meson.build                      registers the plugin in the fwupd
                                     build
```

The fork's flake.nix provides:

- `fwupd-configure` — one-time meson setup with the right options.
- `fwupd-stage-quirks` — rebuilds `builtin.quirk.gz` and stages it into
  `build/_local/lib/fwupd/quirks.d/` so iterative `meson compile -C build`
  cycles see the latest quirk.
- `nix build` — full clean fwupd derivation with our plugin baked in,
  inheriting nixpkgs' build closure via `inputsFrom`.

## Reference material on this machine

| Artefact | Path |
|----------|------|
| Original `.deb` (M3T105 Ubuntu) | `/agents/ada/projects/dell-u4025qw-fw/U4025QW_M3T105.deb` |
| Extracted contents | `/agents/ada/projects/dell-u4025qw-fw/extracted/` |
| Main updater binary (Sciter shell) | `…/Firmware Updater` |
| RealTek burn library (UNSTRIPPED) | `…/librtburn.so` |
| Per-subsystem plugins | `…/plugins/lib{display,hub,mcu,pdc,tbt,touch,webcam,audio,devices,bridge}.so` |
| Firmware payload (LGD panel) | `…/M3T105/DELL_U4025QW_LGD_4FCF2_M3T105_20251009.upg` |
| .deb udev rules | `…/etc/udev/rules.d/99-monitorfirmwareupdateutility-U4025QW.rules` |
| Successful update USB capture | `/agents/ada/projects/dell-u4025qw-fw/captures/u4025qw-m3t105-update-171436.pcapng` (155 MB) |
| Pre-update enumeration noise capture | `/agents/ada/projects/dell-u4025qw-fw/captures/u4025qw-m3t105-20260430-170506-noise.pcapng` (13 MB) |

External references:

- Dell U4025QW Firmware M3T105 (Ubuntu): <https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=nvpvj>
- fwupd Plugin Tutorial: <https://fwupd.github.io/libfwupdplugin/tutorial.html>
- fwupd policy on proprietary code: same page as above
- LVFS Custom Protocol docs: <https://lvfs.readthedocs.io/en/latest/custom-plugin.html>
- fwupd discussion on Dell monitor support: <https://github.com/fwupd/fwupd/discussions/8189>
- `realtek-mst` plugin (related but different chips): <https://fwupd.github.io/libfwupdplugin/realtek-mst-README.html>
- `mediatek-scaler` plugin (closest in spirit): <https://fwupd.github.io/libfwupdplugin/mediatek-scaler-README.html>
