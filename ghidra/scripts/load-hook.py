"""
gdb-Python script: hook every plugin .so's exported `load` function
(the one main calls to push a per-component firmware blob into the
plugin) and dump the bytes it receives.

Sourced into gdb via:
  (gdb) source /agents/ada/projects/dell-u4025qw-fw/ghidra/scripts/load-hook.py

`load` is exported by every plugin .so (libhub.so, libdevices.so,
libpdc.so, libdisplay.so, ...). Its signature in the plugin source is
something like:
   uint32_t load(MODULE_INTF *self, std::vector<uint8_t> const &blob);
which in x86_64 SysV ends up as:
   rdi = self pointer (plugin instance)
   rsi = pointer to vector header { void* data; size_t size; size_t cap; }
The vector's data pointer is at +0, size at +8.

We hook on `load` (a plain C symbol — no name mangling), at every
plugin .so. At entry we dump:
  - which .so / address the call hit
  - the size of the blob (rsi[1])
  - the first 64 bytes of blob data
  - the SHA-256 of the full blob (so we can correlate against the .upg)
  - a backtrace
We do NOT step out / read the return value — load() may run for a
while and we don't want to interfere.

Output goes to /tmp/load-hook-trace.log AND stdout. Append-mode; truncate
manually for a fresh capture.
"""

import gdb
import hashlib

LOG_PATH = "/tmp/load-hook-trace.log"
_logfile = open(LOG_PATH, "a", buffering=1)


def log(msg):
    print(msg)
    _logfile.write(msg + "\n")


def read_bytes(addr, n):
    inf = gdb.selected_inferior()
    try:
        return inf.read_memory(addr, n).tobytes()
    except gdb.MemoryError as e:
        return None


def memory_provenance(addr):
    try:
        out = gdb.execute(f"info symbol {addr:#x}", to_string=True).strip()
        if "No symbol" not in out:
            return out
    except gdb.error:
        pass
    try:
        for line in gdb.execute("info proc mappings", to_string=True).splitlines():
            parts = line.split()
            if len(parts) >= 5:
                try:
                    s = int(parts[0], 16); e = int(parts[1], 16)
                    if s <= addr < e:
                        return f"in {parts[-1]} ({parts[0]}-{parts[1]})"
                except ValueError:
                    pass
    except gdb.error:
        pass
    return f"<no mapping for {addr:#x}>"


class LoadHook(gdb.Breakpoint):
    """Fires on every entry to plugin->load() in any loaded .so."""
    def __init__(self):
        super().__init__("load", type=gdb.BP_BREAKPOINT, internal=False)
        self.silent = True

    def stop(self):
        try:
            rdi = int(gdb.parse_and_eval("(unsigned long long)$rdi"))
            rsi = int(gdb.parse_and_eval("(unsigned long long)$rsi"))
            rdx = int(gdb.parse_and_eval("(unsigned long long)$rdx"))
            pc  = int(gdb.parse_and_eval("(unsigned long long)$pc"))

            log("\n=================================================")
            log(f"load() @ {pc:#x}  ({memory_provenance(pc)})")
            log(f"  rdi = {rdi:#x}  ({memory_provenance(rdi)})")
            log(f"  rsi = {rsi:#x}  ({memory_provenance(rsi)})")
            log(f"  rdx = {rdx:#x}")

            # Try BOTH common signatures:
            #
            # (A) load(void *data, size_t size)
            #     rdi = data_ptr, rsi = size
            # (B) load(MODULE_INTF *self, vector<uint8_t> const& blob)
            #     rdi = self, rsi = vector_header_ptr → 24 bytes (data, end, cap)
            #
            # Heuristic: if rsi looks like a small integer (< 16 MB) AND rdi
            # looks like a valid pointer with readable bytes, it's (A).
            # Otherwise probe rsi as a vector header.

            head_a = read_bytes(rdi, 64)
            head_a_ok = head_a is not None and head_a != b"\x00" * 64

            looks_like_size_a = (0 < rsi < 16 * 1024 * 1024) and head_a_ok
            if looks_like_size_a:
                log(f"  → interpreting as load(data, size): size={rsi}")
                head = read_bytes(rdi, min(rsi, 64)) or b""
                full = read_bytes(rdi, rsi) if 0 < rsi <= 4 * 1024 * 1024 else None
                digest = hashlib.sha256(full).hexdigest() if full else "<not read>"
                log(f"  data sha256 = {digest}")
                log(f"  head[64]    = {head.hex()}")
                log(f"  ascii       = {head[:64].decode('ascii', errors='replace')!r}")
                # Also dump the FULL data to a file for analysis
                if full and rsi > 1024:
                    out_path = f"/tmp/load-data-{pc:x}-sz{rsi}.bin"
                    open(out_path, "wb").write(full)
                    log(f"  full data → {out_path}")
            else:
                # Try as vector
                hdr = read_bytes(rsi, 24)
                if hdr is None:
                    log(f"  rsi as vector_hdr: unreadable")
                else:
                    data_ptr = int.from_bytes(hdr[0:8], "little")
                    end_ptr  = int.from_bytes(hdr[8:16], "little")
                    cap_ptr  = int.from_bytes(hdr[16:24], "little")
                    size = end_ptr - data_ptr
                    if 0 < size < 16 * 1024 * 1024:
                        log(f"  → interpreting as load(self, vector): size={size}")
                        head = read_bytes(data_ptr, min(size, 64)) or b""
                        full = read_bytes(data_ptr, size) if size <= 4 * 1024 * 1024 else None
                        digest = hashlib.sha256(full).hexdigest() if full else "<not read>"
                        log(f"  data ptr  = {data_ptr:#x}")
                        log(f"  size      = {size} bytes ({size/1024:.1f} KB)")
                        log(f"  sha256    = {digest}")
                        log(f"  head[64]  = {head.hex()}")
                        log(f"  ascii     = {head[:64].decode('ascii', errors='replace')!r}")
                        if full and size > 1024:
                            out_path = f"/tmp/load-vec-{pc:x}-sz{size}.bin"
                            open(out_path, "wb").write(full)
                            log(f"  full data → {out_path}")
                    else:
                        log(f"  rsi as vector_hdr: implausible size {size}")

            bt = gdb.execute("bt 6", to_string=True)
            log(f"  backtrace:")
            for line in bt.splitlines()[:6]:
                log(f"    {line}")

            return False  # don't stop, continue automatically
        except Exception as e:
            log(f"  <hook error: {e}>")
            return False


gdb.execute("set breakpoint pending on")
LoadHook()
log(">>> load() hook armed; output → " + LOG_PATH)
