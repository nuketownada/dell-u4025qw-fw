# Tracing `decrypt_string` calls in Dell's Firmware Updater

Goal: dump every (passphrase, encrypted_input, decrypted_output, backtrace)
tuple as Dell's binary calls its CryptoPP-based `decrypt_string` helper.
Once we have a few examples we can:

1. See whether the passphrase is `cert2.key` directly, an embedded
   literal, or derived from somewhere else (provenance + backtrace
   make this obvious).
2. Reimplement `decrypt_string` ourselves with the now-known passphrase
   and validate against the captured plaintext.
3. Use the resulting decryptor to statically dump every plugin .so's
   chip-type LUT — the slot→chip mapping we need for the plugin.
4. As a side-effect, decrypt `appconfig.dat` (almost certainly the
   same scheme).

## Run

```sh
cd /agents/ada/projects/dell-u4025qw-fw/extracted/usr/share/Dell/firmware/U4025QW

# We want a graphical session because the binary is a Sciter GUI app.
# Make sure DISPLAY is set; if running as ada with josh's X session,
# use the same xauthority trick we used for the original capture.

# Truncate the log if you want a fresh run (otherwise it appends):
: > /tmp/decrypt-string-trace.log

gdb ./Firmware\ Updater <<'EOF'
set pagination off
set confirm off
source /agents/ada/projects/dell-u4025qw-fw/ghidra/scripts/decrypt-string-trace.py
run
EOF
```

You only need the binary to reach plugin-discovery + chip-type init,
which happens at startup BEFORE any device interaction. So the monitor
doesn't need to be plugged in; even crashing immediately after init
(because no device is found) gives us the data we want.

## Reading the output

`/tmp/decrypt-string-trace.log` has one block per `decrypt_string`
call. The key field is `passphrase`:

  - **Matches `a381eba3c5fa0c49ac48eec32fcc2e25`?** → it's `cert2.key`
    decoded from the Base64URL section of `cert2.dat`. We're done with
    derivation; just need to figure out the exact CryptoPP wire format.
  - **Looks like a plain ASCII literal?** → embedded passphrase, see
    the `passphrase ascii` field for the value.
  - **Random-looking 16/24/32 bytes?** → derived (likely HKDF/PBKDF
    from `cert2.key` + some info string). Look at the `backtrace` to
    find the calling function, then at that function's Ghidra decompile
    to see the derivation chain (info string, salt, iteration count,
    etc.).

The `provenance` lines tell us where in the address space each pointer
comes from — useful for distinguishing stack-local strings (built at
runtime) from `.rodata`-resident literals.

## What the rest of the output tells us

  - `encrypted ascii`: the input ciphertext (Base64URL-encoded). Many
    of these are short — chip-type names like "PARADE FL5500 IIC"
    encrypted to maybe ~30-40 chars of Base64URL.
  - `decrypted`: the resulting plaintext, captured by stepping out of
    the function with `finish`. This is our ground truth — we can use
    these (input, output) pairs to validate any reimplementation.

## Caveats

  - `decrypt_string` is a STATIC function in each .so (libhub.so,
    libdevices.so, etc.), so the breakpoint by mangled name resolves
    to multiple addresses; that's fine, gdb sets all of them.
  - `finish` from inside a `Breakpoint.stop()` callback is allowed but
    can be flaky if the breakpoint is hit recursively. If we see
    spurious `<finish failed>` lines, switch to a separate
    return-address breakpoint.
  - The Sciter GUI may try to auto-update or do other annoying things.
    If it tries to phone home, we can run with no network or kill it
    after we've collected enough samples.
