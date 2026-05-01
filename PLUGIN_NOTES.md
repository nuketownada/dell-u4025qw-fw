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
| 87,352  | `c0 f3 00 00`      | Status poll (tight loop) |
|  4,077  | `40 c6 00 00`      | TBD — likely version/info or bootloader-enter |
|  2,589  | `40 d6 60 00`      | `0xd6` subcommand family (probably erase-related) |
|  1,840  | `40 d6 08 00`      | (same family) |
|  1,832  | `40 d6 09 00`      | (same family) |
|  1,607  | `40 d6 6f 00`      | (same family) |
|     27  | `40 d6 75 00`      | (same family) |
|     26 each × 16  | `40 f1 80 f0..ff` | **Flash write loop**, sweeping low addr byte |
|     26  | `40 f5 94 01`      | TBD |
|     26  | `40 f4 00 00`      | TBD |

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

### Disassembly of `librtburn.so` to confirm opcode names

> Goal: walk each `hid_*` function, identify which `bmRequestType / wValue /
> wLength` SETUP it emits and which 192-byte payload it builds, then bind
> a human-readable name to every opcode in the histogram above.

- [ ] `hid_GetUSBFwVersion` → opcode `?`
- [ ] `hid_fwUSBBootloader` → opcode `?`
- [ ] `hid_fwUpdateUSB` chunk format (header layout for `40 f1 …`)
- [ ] `hid_GetUSBUpdateFWProgress` response interpretation
- [ ] `EraseBank` and the `script_*` constants (precanned byte sequences?)
- [ ] `hid_CheckECDSA` — input format and which key is used to verify
- [ ] `hid_fwReboot` — exact opcode and post-reboot timing

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

### `.upg` format details

- [ ] Exact location of each component's binary payload within the file
      (where do the ASCII signature blocks end and binary bytes begin?)
- [ ] Which records correspond to the `HUB*`, `PDC`, `DISPLAY`, `DISPLAY`
      (twice) component sections? Are HUB1/2/4 sub-firmwares of the
      USB hub MCU, or different physical devices on the monitor?
- [ ] What is the trailing `UPDATE_BY_SCALER` record telling the
      updater? A delivery-method discriminator (write via scaler vs
      via dedicated MCU)?
- [ ] What hashes/keys do `cert.dat` (18 bytes) and `cert2.dat`
      (26 bytes ASCII) encode?
- [ ] What is in `appconfig.dat`? Is it actually encrypted or just
      a binary structure with no ASCII?

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
