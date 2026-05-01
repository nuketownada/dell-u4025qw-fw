# captures/

Binary blobs and logs from runtime gdb instrumentation of Dell's
Firmware Updater binary.

## load-data-*.bin and load-vec-*.bin

Captured via `ghidra/scripts/safe-trace.py` — bytes that Dell's main
binary passed to plugin->load() functions during a controlled gdb run
that auto-killed before any device write.

For the U4025QW M3T105 .upg:
- `load-data-*-sz71848.bin`: ENCRYPTED .upg slice handed to libhub's
  C `load(data, size)` entry point. Entropy 7.997, appears verbatim
  in the .upg's binary section.
- `load-vec-*-sz131072.bin`: PLAINTEXT firmware (128 KB) handed to
  `Rts5409s_IIC_ISP::load(vector)` after libhub's `Certify::c()`
  decrypts and gunzips. Entropy 6.202, contains long 0xff runs
  (uninitialized flash) and bytes that match `40 f1` SRAM-write
  payloads from the actual update pcap.

## safe-trace.log

Per-call trace of every load() and decrypt_string() invocation, plus
the panic-hook hit that auto-killed gdb before any device mutation.
