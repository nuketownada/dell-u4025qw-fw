"""
gdb-Python script: safely observe Dell's Firmware Updater while it
runs against a real monitor.

Behavior:
  * `load()` hooks (LoadHook): silent — dump the per-component blob
    bytes that main passes to plugin->load(...) and continue. These
    are pure RAM operations; no device state changes.
  * `decrypt_string()` hooks (DecryptStringHook): silent — record
    every (passphrase, encrypted, plaintext) tuple and continue.
  * "Panic" hooks (PanicHook): STOP gdb on the first call to ANY
    function that actually mutates the device (bootloader-enter,
    flash erase, SRAM-to-SPI commit, hub flash write, ...). Prints
    a loud warning and lets the user inspect state, then `kill` or
    `quit` from the gdb prompt to abort cleanly.

The list of panic functions is conservative: it covers every "this
will modify the device's flash/state" entry point we identified in
libhub.so, libdevices.so, libpdc.so, libdisplay.so. Read-only ops
(get_fw_version, set_bus_speed, enable_vdcmd, the 0xE1 cal_auth
handshake, DDC/CI reads, etc.) are NOT in the list — those run
freely so Dell's binary can do its discovery / version-read /
metadata-decryption work.

Sourced via:
  (gdb) source /agents/ada/projects/dell-u4025qw-fw/ghidra/scripts/safe-trace.py
"""

import gdb
import hashlib

LOG_PATH = "/tmp/safe-trace.log"
_logfile = open(LOG_PATH, "a", buffering=1)


def log(msg):
    print(msg)
    _logfile.write(msg + "\n")


def read_bytes(addr, n):
    inf = gdb.selected_inferior()
    try:
        return inf.read_memory(addr, n).tobytes()
    except gdb.MemoryError:
        return None


def read_cstring(addr, max_len=256):
    b = read_bytes(addr, max_len)
    if b is None:
        return None
    nul = b.find(b"\x00")
    if nul >= 0:
        b = b[:nul]
    return b.decode("ascii", errors="replace")


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
                        return f"in {parts[-1]}"
                except ValueError:
                    pass
    except gdb.error:
        pass
    return f"<no mapping for {addr:#x}>"


# --------------------------------------------------------------------------
# Silent observation hooks
# --------------------------------------------------------------------------


class LoadHook(gdb.Breakpoint):
    """Plugin-export `load(...)`: dump bytes and continue."""

    def __init__(self):
        super().__init__("load", type=gdb.BP_BREAKPOINT, internal=False)
        self.silent = True

    def stop(self):
        try:
            rdi = int(gdb.parse_and_eval("(unsigned long long)$rdi"))
            rsi = int(gdb.parse_and_eval("(unsigned long long)$rsi"))
            pc  = int(gdb.parse_and_eval("(unsigned long long)$pc"))

            log(f"\n--- load() @ {pc:#x}  ({memory_provenance(pc)})")
            # Try (data, size) signature first.
            looks_like_size = (0 < rsi < 16 * 1024 * 1024)
            head_a = read_bytes(rdi, 64) if looks_like_size else None
            if looks_like_size and head_a is not None and head_a != b"\x00" * 64:
                size = rsi
                log(f"  [data, size]: rdi={rdi:#x}  size={size}")
                head = read_bytes(rdi, min(size, 64)) or b""
                full = read_bytes(rdi, size) if size <= 4 * 1024 * 1024 else None
                log(f"  head        = {head.hex()}")
                log(f"  ascii       = {head[:64].decode('ascii', errors='replace')!r}")
                if full and size > 1024:
                    out_path = f"/tmp/load-data-{pc:x}-sz{size}.bin"
                    open(out_path, "wb").write(full)
                    log(f"  full → {out_path}  sha256={hashlib.sha256(full).hexdigest()}")
            else:
                # Try (self, vector<uint8_t>&) signature.
                hdr = read_bytes(rsi, 24)
                if hdr is None:
                    log(f"  rdi={rdi:#x}  rsi={rsi:#x}  (both decode attempts failed)")
                else:
                    data_ptr = int.from_bytes(hdr[0:8], "little")
                    end_ptr  = int.from_bytes(hdr[8:16], "little")
                    size = end_ptr - data_ptr
                    if 0 < size < 16 * 1024 * 1024:
                        log(f"  [self, vector]: self={rdi:#x}  data={data_ptr:#x}  size={size}")
                        head = read_bytes(data_ptr, min(size, 64)) or b""
                        log(f"  head        = {head.hex()}")
                        log(f"  ascii       = {head[:64].decode('ascii', errors='replace')!r}")
                        if size > 1024:
                            full = read_bytes(data_ptr, size) if size <= 4 * 1024 * 1024 else None
                            if full:
                                out_path = f"/tmp/load-vec-{pc:x}-sz{size}.bin"
                                open(out_path, "wb").write(full)
                                log(f"  full → {out_path}  sha256={hashlib.sha256(full).hexdigest()}")
                    else:
                        log(f"  vector size {size} implausible; skipping")
            return False
        except Exception as e:
            log(f"  <load hook error: {e}>")
            return False


class DecryptStringHook(gdb.Breakpoint):
    """Each decrypt_string call: dump (passphrase, ciphertext) and continue."""

    def __init__(self):
        super().__init__("_Z14decrypt_stringPKcS0_", type=gdb.BP_BREAKPOINT,
                         internal=False)
        self.silent = True

    def stop(self):
        try:
            rdi = int(gdb.parse_and_eval("(unsigned long long)$rdi"))
            rsi = int(gdb.parse_and_eval("(unsigned long long)$rsi"))
            pc  = int(gdb.parse_and_eval("(unsigned long long)$pc"))
            passphrase = read_cstring(rdi, 128) or "<unreadable>"
            encrypted  = read_cstring(rsi, 1024) or "<unreadable>"
            log(f"\n--- decrypt_string @ {pc:#x}  ({memory_provenance(pc)})")
            log(f"  passphrase = {passphrase!r}")
            log(f"  encrypted  = {encrypted[:120]!r}{'...' if len(encrypted) > 120 else ''}")
            return False
        except Exception as e:
            log(f"  <decrypt hook error: {e}>")
            return False


# --------------------------------------------------------------------------
# Panic hooks: STOP gdb on first device-mutating call.
# --------------------------------------------------------------------------


# Mangled symbol → human label. EVERY one of these mutates the device
# in some way. Run-time hits should be considered "we MUST stop here
# before this completes; killing the process is the safe choice".
PANIC_SYMBOLS = [
    # libdevices.so (RTS5409S_HID — upstream hub MCU)
    ("_ZN12RTS5409S_HID14reset_to_flashEv",
        "RTS5409S_HID::reset_to_flash — BOOTLOADER ENTER"),
    ("_ZN12RTS5409S_HID10reset_selfEv",
        "RTS5409S_HID::reset_self — device reset (post-update)"),
    ("_ZN12RTS5409S_HID16erase_spare_bankEv",
        "RTS5409S_HID::erase_spare_bank — ERASES SPARE FLASH"),
    ("_ZN12RTS5409S_HID15erase_tmp_flashEv",
        "RTS5409S_HID::erase_tmp_flash — ERASES TEMP FLASH"),
    ("_ZN12RTS5409S_HID15write_hub_flashEjPKhh",
        "RTS5409S_HID::write_hub_flash — WRITES TO HUB FLASH"),
    ("_ZN12RTS5409S_HID15write_tmp_flashEjPKhh",
        "RTS5409S_HID::write_tmp_flash — WRITES TO TEMP FLASH"),
    ("_ZN12RTS5409S_HID22write_hub_container_idERK11_hub_info_t",
        "RTS5409S_HID::write_hub_container_id"),
    ("_ZN12RTS5409S_HID19write_hub_serial_noERK11_hub_info_t",
        "RTS5409S_HID::write_hub_serial_no"),
    ("_ZN12RTS5409S_HID11update_selfERK8PROG_CFG",
        "RTS5409S_HID::update_self — UPDATES SELF FW"),
    ("_ZN12RTS5409S_HID15update_hub_infoERK8PROG_CFG",
        "RTS5409S_HID::update_hub_info"),
    ("_ZN12RTS5409S_HID14secure_programERK15SECURE_PROG_CFG",
        "RTS5409S_HID::secure_program"),
    ("_ZN12RTS5409S_HID11tbt_programEhjPhh",
        "RTS5409S_HID::tbt_program"),
    ("_ZN12RTS5409S_HID19secure_control_gpioEhh",
        "RTS5409S_HID::secure_control_gpio"),

    # libhub.so (FL5500_IIC — scaler chip)
    ("_ZN14FL5500_IIC_ISP3ispEv",
        "FL5500_IIC_ISP::isp — TOP-LEVEL UPDATE ENTRY"),
    ("_ZN14FL5500_IIC_ISP10update_hubEv",
        "FL5500_IIC_ISP::update_hub — orchestrates flash writes"),
    ("_ZN14FL5500_IIC_API17sram_to_spi_flashEjPhii",
        "FL5500_IIC_API::sram_to_spi_flash — COMMITS SECTOR TO FLASH"),
    ("_ZN14FL5500_IIC_API16write_spi_sectorEPhi",
        "FL5500_IIC_API::write_spi_sector"),
    ("_ZN14FL5500_IIC_API23trigger_write_spi_flashEv",
        "FL5500_IIC_API::trigger_write_spi_flash"),
    ("_ZN14FL5500_IIC_API9reset_hubEv",
        "FL5500_IIC_API::reset_hub"),

    # libhub.so (Rts5409s_IIC — RealTek hub via I²C)
    ("_ZN16Rts5409s_IIC_ISP3ispEv",
        "Rts5409s_IIC_ISP::isp — TOP-LEVEL UPDATE ENTRY"),
    ("_ZN16Rts5409s_IIC_ISP7programEv",
        "Rts5409s_IIC_ISP::program — programs flash"),
    ("_ZN16Rts5409s_IIC_API11write_flashEPhh",
        "Rts5409s_IIC_API::write_flash — WRITES TO FLASH"),
    ("_ZN16Rts5409s_IIC_API10soft_resetEv",
        "Rts5409s_IIC_API::soft_reset"),
]


class PanicHook(gdb.Breakpoint):
    """STOP gdb on entry. Returns True from stop() to halt the inferior."""

    def __init__(self, sym, label):
        super().__init__(sym, type=gdb.BP_BREAKPOINT, internal=False)
        self.label = label

    def stop(self):
        pc = int(gdb.parse_and_eval("(unsigned long long)$pc"))
        log("\n" + "!" * 60)
        log(f"!!! PANIC HOOK fired: {self.label}")
        log(f"!!! pc = {pc:#x}  ({memory_provenance(pc)})")
        log("!!! gdb is now STOPPED. Inspect with `bt`, `info reg`, etc.")
        log("!!! To abort safely:  type `kill` then `quit` at the gdb prompt.")
        log("!" * 60)
        try:
            bt = gdb.execute("bt 8", to_string=True)
            log("backtrace at panic:")
            for line in bt.splitlines()[:8]:
                log(f"  {line}")
        except gdb.error:
            pass
        return True   # actually stop


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------


gdb.execute("set breakpoint pending on")
gdb.execute("set print pretty on")

LoadHook()
DecryptStringHook()
for sym, label in PANIC_SYMBOLS:
    try:
        PanicHook(sym, label)
    except Exception as e:
        log(f">>> failed to arm panic hook on {sym}: {e}")

log(">>> safe-trace armed: load() + decrypt_string() observed silently;")
log(">>> any device-mutating call HALTS gdb.")
log(f">>> log → {LOG_PATH}")
