# Ghidra headless decompile workflow

We decompile each binary in Dell's monitorfirmwareupdateutility .deb
to plain C and grep through the result. This makes navigation 10×
faster than `objdump | grep` on the raw .so files.

## Setup

Everything goes through the repo's flake (Ghidra 12 from
nixpkgs-unstable, pinned in `flake.lock`):

```sh
cd /path/to/dell-u4025qw-fw

# Dev shell with all tooling on PATH:
nix develop

# Or one-shot invocations without entering the shell:
nix run .#ghidra                  # GUI
nix run .#analyze -- <binary>     # import + analyze + RTTI recovery
nix run .#decompile -- <binary>   # re-decompile to ghidra/decomp/<name>.c
nix run .#ghidra-analyzeHeadless  # raw headless escape hatch
```

The `analyze` / `decompile` wrappers know the project layout — pass
just the relative binary path/basename, not full paths.

## Typical flow

```sh
# First time on a new binary: import + full analysis pass.
nix run .#analyze -- plugins/libdisplay.so

# After tweaking a script, or to refresh the dumped C from the
# already-analyzed project (fast, no re-analysis):
nix run .#decompile -- libdisplay.so

# Browse: every function is bracketed by a marker comment.
bash ghidra/scripts/find-fn.sh ghidra/decomp/libdisplay.c \
    "REALTEK_API::spi_unit_erase"
```

The Ghidra project DB lives in `ghidra/project/` (large, binary,
.gitignored — regenerable via `analyze`). The decompile outputs in
`ghidra/decomp/*.c` ARE checked in (text-compressible, expensive to
regenerate, valuable as documentation cross-references).

## Class recovery

`analyze` runs Ghidra's built-in `RecoverClassesFromRTTIScript`
during the analysis pass. This recovers vftable layouts and names
them in the decomp (e.g. `CryptoPP::SHA3::vftable` instead of
`&PTR__SHA3_003345b8`). However:

- The built-in script's GCC class recovery is labeled "early stages
  of development" in its own docstring and **skips classes with
  virtual inheritance** — which is most of Wistron's chip classes
  (IIC_INTF / HID_INTF base ⇄ RTS5409S_HID / FL5500_IIC_API / …
  overrides). So vtable-dispatch calls inside those classes stay as
  raw `*(code **)(lVar5 + 0x120)` indirect calls rather than
  resolving to named member-function calls.
- The standalone `Ghidra-Cpp-Class-Analyzer` extension was designed
  to fix exactly this, but the upstream (astrelsky) was archived in
  Oct 2023, and the only active fork (Fancy2209) hasn't completed
  the port to Ghidra 12 — `./nix/cpp-class-analyzer.nix` builds
  cleanly through the gradle setup but hits 30 API-mismatch compile
  errors. Worth revisiting if a fork catches up or we invest in
  patching it.

For now: vtable resolution is partial; member-function dispatch in
hand-written wrapper classes requires manual tracing.

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
