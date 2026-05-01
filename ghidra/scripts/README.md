# Ghidra headless decompile workflow

We decompile each binary in Dell's monitorfirmwareupdateutility .deb to
plain C and grep through the result. This makes navigation 10× faster
than `objdump | grep` on the raw .so files.

## Setup

Ghidra ships in nixpkgs:

```sh
nix run nixpkgs#ghidra
```

The headless driver is `ghidra-analyzeHeadless`.

## One-shot decompile

```sh
GHIDRA=$(nix-build '<nixpkgs>' -A ghidra --no-out-link)/bin/ghidra-analyzeHeadless

# Pick one of: libdevices.so, libhub.so, libdisplay.so, libpdc.so, "Firmware Updater"
TARGET=plugins/libhub.so
OUT=$(basename "$TARGET" .so).c

$GHIDRA \
  ../project DellMonitorRT \
  -import "../../extracted/usr/share/Dell/firmware/U4025QW/$TARGET" \
  -scriptPath . \
  -postScript DumpDecompiled.java "../decomp/$OUT" \
  -overwrite
```

The Ghidra DB lives in `../project/`; .gitignored because it's large
and binary. The decompile outputs live in `../decomp/*.c` and ARE
checked in (text-compressible, expensive to regenerate, valuable as
documentation).

## Probing decryption schemes for appconfig.dat

```sh
nix-shell -p 'python3.withPackages (ps: with ps; [ pycryptodome ])' \
  --run 'python3 decrypt-appconfig.py'
```

Status: no scheme has cracked appconfig.dat yet. Likely needs further
Ghidra work to identify the exact KDF / mode / passphrase used by the
main `Firmware Updater` binary. See PLUGIN_NOTES "Plugin status" for
why this isn't blocking — the slot→chip mapping turned out to be
hardcoded in the per-slot interface .cpp files inside the main
binary, not in `appconfig.dat`.

## Helpers

- `find-fn.sh <decomp.c> <pattern>` — pull a single decompiled
  function (and its signature) out of a dump. Each function is
  bracketed by `// ===== <name> @ <addr> =====`.
