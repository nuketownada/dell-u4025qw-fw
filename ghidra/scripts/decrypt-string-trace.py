"""
gdb-Python script: trace every call to decrypt_string() across all loaded
plugin .so files and dump (passphrase, encrypted, decrypted, backtrace,
provenance) for each.

Sourced into gdb via:
  (gdb) source /agents/ada/projects/dell-u4025qw-fw/ghidra/scripts/decrypt-string-trace.py

The function is a CryptoPP wrapper:
  std::string decrypt_string(char const* passphrase, char const* encrypted_b64)
returning a pointer to a function-static std::string. We hook it with
gdb's pending breakpoint mechanism so the breakpoint fires once each .so
is loaded.

Output goes to /tmp/decrypt-string-trace.log AND to stdout. The log is
appended (so multiple runs accumulate; truncate manually if you want a
fresh capture).
"""

import gdb

LOG_PATH = "/tmp/decrypt-string-trace.log"
_logfile = open(LOG_PATH, "a", buffering=1)


def log(msg):
    print(msg)
    _logfile.write(msg + "\n")


def read_cstring(addr, max_len=256):
    """Read a NUL-terminated string from inferior memory."""
    inf = gdb.selected_inferior()
    try:
        data = inf.read_memory(addr, max_len).tobytes()
    except gdb.MemoryError as e:
        return f"<unreadable @ {addr:#x}: {e}>"
    nul = data.find(b"\x00")
    if nul >= 0:
        data = data[:nul]
    return data.decode("ascii", errors="replace")


def read_bytes(addr, length):
    inf = gdb.selected_inferior()
    try:
        return inf.read_memory(addr, length).tobytes()
    except gdb.MemoryError as e:
        return None


def memory_provenance(addr):
    """Return a short string identifying which mapping/section addr lives in."""
    try:
        out = gdb.execute(f"info symbol {addr:#x}", to_string=True).strip()
    except gdb.error:
        out = ""
    if out and "No symbol" not in out:
        return out
    # Fall back to the mapping table.
    try:
        mappings = gdb.execute("info proc mappings", to_string=True).splitlines()
        for line in mappings:
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                start = int(parts[0], 16)
                end = int(parts[1], 16)
            except ValueError:
                continue
            if start <= addr < end:
                return f"in {parts[-1]} ({parts[0]}-{parts[1]})"
    except gdb.error:
        pass
    return f"<no mapping found for {addr:#x}>"


def read_std_string(addr):
    """
    Decode a libstdc++ std::string at `addr`.

    Layout (libstdc++ since GCC 5):
      offset 0: char* _M_dataplus._M_p   (points to data, may be inline buf)
      offset 8: size_t _M_string_length
      offset 16: union { char[16] inline_buf; size_t allocated_capacity; }
    """
    raw = read_bytes(addr, 32)
    if raw is None:
        return None
    data_ptr = int.from_bytes(raw[0:8], "little")
    length = int.from_bytes(raw[8:16], "little")
    if length > 0x10000 or data_ptr == 0:
        return None
    data = read_bytes(data_ptr, min(length, 4096))
    if data is None:
        return None
    return data


def hex_dump(b, max_bytes=64):
    if b is None:
        return "<null>"
    return b[:max_bytes].hex()


class DecryptStringEntry(gdb.Breakpoint):
    """Fires on every entry to decrypt_string in any loaded .so."""

    def __init__(self, location):
        super().__init__(location, type=gdb.BP_BREAKPOINT, internal=False)
        self.silent = True

    def stop(self):
        try:
            rdi = int(gdb.parse_and_eval("(unsigned long long)$rdi"))
            rsi = int(gdb.parse_and_eval("(unsigned long long)$rsi"))
            pc = int(gdb.parse_and_eval("(unsigned long long)$pc"))

            passphrase_str = read_cstring(rdi, 256)
            passphrase_bytes = read_bytes(rdi, 32)
            encrypted_str = read_cstring(rsi, 1024)
            pp_prov = memory_provenance(rdi)
            enc_prov = memory_provenance(rsi)
            self_prov = memory_provenance(pc)

            log("\n========================================")
            log(f"decrypt_string @ {pc:#x}  ({self_prov})")
            log(f"  passphrase  ptr={rdi:#x}  ({pp_prov})")
            log(f"  passphrase  ascii: {passphrase_str!r}")
            log(f"  passphrase  bytes: {hex_dump(passphrase_bytes)}")
            log(f"  encrypted   ptr={rsi:#x}  ({enc_prov})")
            log(f"  encrypted   ascii: {encrypted_str!r}")

            # Backtrace, top 6 frames.
            bt = gdb.execute("bt 6", to_string=True)
            log("  backtrace:")
            for line in bt.splitlines()[:6]:
                log(f"    {line}")

            # Step out to capture the return value.
            try:
                gdb.execute("finish", to_string=True)
            except gdb.error as e:
                log(f"  <finish failed: {e}>")
                return False

            rax = int(gdb.parse_and_eval("(unsigned long long)$rax"))
            decrypted_bytes = read_std_string(rax)
            if decrypted_bytes is not None:
                log(f"  decrypted   ({len(decrypted_bytes)} bytes): {decrypted_bytes!r}")
            else:
                log(f"  decrypted   <couldn't decode std::string @ {rax:#x}>")

            return False  # never stop, just trace
        except Exception as e:
            log(f"  <hook error: {e}>")
            return False


# Set both pending and explicit-by-mangled-name forms — whichever resolves first wins.
gdb.execute("set breakpoint pending on")
gdb.execute("set print pretty on")
DecryptStringEntry("_Z14decrypt_stringPKcS0_")
log(">>> decrypt_string trace armed. Run the program; output → " + LOG_PATH)
