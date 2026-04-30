# dell-u4025qw-fw

Reverse-engineering notes and data for a future `fwupd` plugin that updates
the firmware on Dell monitors built around RealTek scaler controllers and
Wistron's "ISP" updater architecture (reference monitor: Dell U4025QW).

See **[PLUGIN_NOTES.md](PLUGIN_NOTES.md)** for the full design document, what
we know, what we still need to learn, and where every artefact lives.

## Layout

```
.
├── PLUGIN_NOTES.md             — design doc + scratchpad
├── U4025QW_M3T105.deb          — Dell's official Ubuntu firmware updater
│                                  (downloaded from dl.dell.com)
├── extracted/                  — `dpkg -x` of the .deb (gitignored)
├── extracted-control/          — `dpkg -e` of the .deb (gitignored)
└── captures/
    ├── u4025qw-m3t105-update-171436.pcapng        — full update USB capture
    └── u4025qw-m3t105-20260430-170506-noise.pcapng — pre-update enumeration
```

## Reproducing the .deb extraction

```sh
nix run nixpkgs#dpkg -- -x U4025QW_M3T105.deb extracted/
nix run nixpkgs#dpkg -- -e U4025QW_M3T105.deb extracted-control/
```

Source URL for the .deb (M3T105, Ubuntu, U4025QW):
<https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=nvpvj>

## Caveats

- `U4025QW_M3T105.deb` is Dell's proprietary binary; it is **not** intended
  for redistribution. Kept in this local repo for offline reference only.
- The captured pcaps contain the firmware payload bytes, which are also
  Dell's. Same caveat applies.
- Should this repo ever become public, both the .deb and the pcaps need
  to be stripped from history first (`git filter-repo --invert-paths …`).
