"""
gdb-Python script: hunt for the EC private key inside libhub.so's
DL_DecryptorImpl object during a real ECIES decryption.

ALWAYS COMBINED WITH the mutation-panic safety net (see PANIC_SYMBOLS
and PanicHook below) — this script never lets a device-mutating call
complete; the FIRST such call halts gdb and auto-kills the inferior.

Strategy: when CryptoPP's `DL_DecryptorBase<ECPPoint>::Decrypt(...)`
is called, `this` (rdi) is the fully-initialized decryptor instance.
The instance holds a `DL_PrivateKey_EC<ECPPoint>` member which in
turn holds the secret scalar as a CryptoPP `Integer`.

CryptoPP `Integer` layout (libstdc++, 64-bit):
  [vtable*][SecBlock<word> reg]   where reg holds word* + size_t
The actual scalar bytes live behind the `SecBlock`'s pointer. For
secp521r1, the scalar is 521 bits → 9 64-bit limbs = 72 bytes.

We can't reliably walk arbitrary C++ vtables from gdb-Python, so
instead we:
  1. dump a sizeable chunk (8 KB) of memory around `this`
  2. write it to disk for offline scanning
  3. ALSO dump every readable pointer-target found in that chunk
     (chasing one level of indirection — covers SecBlock storage)

We also hook:
  - `Certify::d` entry — logs "decryption starting" with input size
  - `DL_DecryptorBase<ECPPoint>::Decrypt` — the actual hook target;
    we dump but DO NOT stop, letting decryption proceed so we can
    correlate our dumped key against the actually-produced plaintext

We let ECIES complete (no panic on Certify) so we can verify the
captured key against the actual plaintext. The mutation-panic chain
is still armed — it fires on the FIRST device-mutating call (e.g.
Rts5409s_IIC_ISP::isp), which happens AFTER Certify::d but BEFORE
any wire write. Auto-kills the inferior at that point.
"""

import gdb
import hashlib
import os

LOG_PATH = "/agents/ada/projects/dell-u4025qw-fw/captures/key-hunter.log"
DUMP_DIR = "/agents/ada/projects/dell-u4025qw-fw/captures"
os.makedirs(DUMP_DIR, exist_ok=True)
_logfile = open(LOG_PATH, "a", buffering=1)


def log(msg):
    print(msg)
    _logfile.write(msg + "\n")
    _logfile.flush()


def write_file(path, data):
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def read_bytes(addr, n):
    inf = gdb.selected_inferior()
    try:
        return inf.read_memory(addr, n).tobytes()
    except gdb.MemoryError:
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
                        return f"in {parts[-1]}"
                except ValueError:
                    pass
    except gdb.error:
        pass
    return f"<no mapping for {addr:#x}>"


def looks_like_pointer(addr):
    """Quick test: is this value a plausible mapped userspace address?"""
    return 0x10000 < addr < 0x800000000000


# Counter so we don't dump the same location 100 times if Decrypt is reentrant.
_decrypt_hits = 0


class CertifyDEntry(gdb.Breakpoint):
    """Certify::d entry — just log."""
    def __init__(self):
        super().__init__("_ZN7Certify1dERKSt6vectorIhSaIhEEPNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEE",
                         type=gdb.BP_BREAKPOINT, internal=False)
        self.silent = True

    def stop(self):
        try:
            rdi = int(gdb.parse_and_eval("(unsigned long long)$rdi"))
            rsi = int(gdb.parse_and_eval("(unsigned long long)$rsi"))
            # rdi = vector const*, rsi = string*
            hdr = read_bytes(rdi, 24)
            if hdr:
                data_ptr = int.from_bytes(hdr[0:8], "little")
                end_ptr = int.from_bytes(hdr[8:16], "little")
                size = end_ptr - data_ptr
                log(f"\n=== Certify::d entered: input vector size={size}")
            else:
                log(f"\n=== Certify::d entered: rdi={rdi:#x} (unreadable)")
        except Exception as e:
            log(f"  <CertifyD hook error: {e}>")
        return False


class PrivateKeyInitHook(gdb.Breakpoint):
    """DL_PrivateKey_EC<ECP>::Initialize(group_params, Integer scalar)
    — fires when an EC private key is constructed from a scalar value.
    Args: rdi=this, rsi=group_params, rdx=Integer*"""

    def __init__(self, sym, label):
        super().__init__(sym, type=gdb.BP_BREAKPOINT, internal=False)
        self.silent = True
        self.label = label

    def stop(self):
        try:
            rdi = int(gdb.parse_and_eval("(unsigned long long)$rdi"))
            rsi = int(gdb.parse_and_eval("(unsigned long long)$rsi"))
            rdx = int(gdb.parse_and_eval("(unsigned long long)$rdx"))
            rcx = int(gdb.parse_and_eval("(unsigned long long)$rcx"))
            r8  = int(gdb.parse_and_eval("(unsigned long long)$r8"))
            log(f"\n*** {self.label}")
            log(f"  this={rdi:#x}  rsi={rsi:#x}  rdx={rdx:#x}  rcx={rcx:#x}  r8={r8:#x}")
            # Try to read each arg as if it were a CryptoPP Integer.
            # Integer layout: [vtable(8)][SecBlock<word> reg]
            #   SecBlock has: word* m_ptr, size_t m_size, AllocBase<word> m_alloc
            # The scalar bytes are at *m_ptr, m_size limbs of 8 bytes each.
            for argname, addr in (("rsi", rsi), ("rdx", rdx), ("rcx", rcx), ("r8", r8)):
                if not looks_like_pointer(addr):
                    continue
                obj = read_bytes(addr, 64)
                if obj is None:
                    continue
                # Heuristic: vtable at offset 0 should be a code-section pointer
                vtable = int.from_bytes(obj[0:8], "little")
                # SecBlock fields at +8 and +16 typically
                m_ptr = int.from_bytes(obj[8:16], "little")
                m_size = int.from_bytes(obj[16:24], "little")
                if not looks_like_pointer(m_ptr) or m_size == 0 or m_size > 16:
                    continue
                limb_bytes = read_bytes(m_ptr, m_size * 8)
                if limb_bytes is None:
                    continue
                # CryptoPP stores Integer as little-endian limbs.
                # Reverse to get big-endian byte representation:
                be = limb_bytes[::-1].lstrip(b"\x00")
                log(f"  arg {argname} → Integer? {m_size} limbs ({m_size*8} bytes)")
                log(f"    raw LE limbs: {limb_bytes.hex()}")
                log(f"    big-endian (no-lead-zero): {be.hex()}  ({len(be)} bytes)")
                # Save Integer-shaped finds to disk for later analysis.
                out_path = f"{DUMP_DIR}/keyhunt-int-{argname}-{m_size}limbs.bin"
                write_file(out_path, limb_bytes)
                log(f"    → saved {out_path}")
        except Exception as e:
            log(f"  <PrivateKeyInit hook error: {e}>")
        return False


class GunzipHook(gdb.Breakpoint):
    """Gunzip::ProcessDecompressedData(byte const*, size_t)
    — fires per chunk of decompressed plaintext."""

    def __init__(self):
        super().__init__("_ZN8CryptoPP6Gunzip23ProcessDecompressedDataEPKhm",
                         type=gdb.BP_BREAKPOINT, internal=False)
        self.silent = True
        self.chunk = 0

    def stop(self):
        try:
            self.chunk += 1
            rdi = int(gdb.parse_and_eval("(unsigned long long)$rdi"))   # this
            rsi = int(gdb.parse_and_eval("(unsigned long long)$rsi"))   # data
            rdx = int(gdb.parse_and_eval("(unsigned long long)$rdx"))   # length
            data = read_bytes(rsi, min(rdx, 256))
            log(f"\n+++ Gunzip chunk #{self.chunk}: {rdx} bytes from {rsi:#x}")
            if data:
                log(f"  head: {data[:64].hex()}")
            # Save the first few chunks to disk.
            if self.chunk <= 16 and rdx > 0:
                full = read_bytes(rsi, rdx)
                if full:
                    out_path = f"{DUMP_DIR}/keyhunt-gunzip-chunk-{self.chunk:03d}.bin"
                    write_file(out_path, full)
        except Exception as e:
            log(f"  <Gunzip hook error: {e}>")
        return False


class DecryptHook(gdb.Breakpoint):
    """DL_DecryptorBase<ECPPoint>::Decrypt entry — dump memory around the
    decryptor object so we can extract the private key offline."""

    SYMBOL = ("_ZNK8CryptoPP16DL_DecryptorBaseINS_8ECPPointEE7Decrypt"
              "ERNS_21RandomNumberGeneratorEPKhmPhRKNS_14NameValuePairsE")

    def __init__(self):
        super().__init__(self.SYMBOL, type=gdb.BP_BREAKPOINT, internal=False)
        self.silent = True

    def stop(self):
        global _decrypt_hits
        _decrypt_hits += 1
        if _decrypt_hits > 4:
            return False  # don't dump more than the first few
        try:
            rdi = int(gdb.parse_and_eval("(unsigned long long)$rdi"))     # this
            rdx = int(gdb.parse_and_eval("(unsigned long long)$rdx"))     # ciphertext ptr
            rcx = int(gdb.parse_and_eval("(unsigned long long)$rcx"))     # ciphertext len
            r8  = int(gdb.parse_and_eval("(unsigned long long)$r8"))      # plaintext ptr (output)

            log("\n" + "=" * 60)
            log(f"=== DL_DecryptorBase<ECPPoint>::Decrypt hit #{_decrypt_hits}")
            log(f"  this           = {rdi:#x}  ({memory_provenance(rdi)})")
            log(f"  ciphertext     = {rdx:#x}, {rcx} bytes")
            log(f"  plaintext_out  = {r8:#x}")

            # Dump 16 KB starting from `this`. This should cover the entire
            # DL_DecryptorImpl<...> object including its embedded DL_PrivateKey_EC.
            DUMP_SIZE = 16 * 1024
            obj_dump = read_bytes(rdi, DUMP_SIZE) or b""
            obj_path = f"{DUMP_DIR}/keyhunt-{_decrypt_hits}-this-{rdi:x}.bin"
            write_file(obj_path, obj_dump)
            log(f"  → wrote {len(obj_dump)} bytes around `this` to {obj_path}")

            # Walk the dumped bytes for plausible 8-byte pointers; for each,
            # follow one level and dump 256 bytes there too. This catches
            # the SecBlock<word>'s backing storage where the private-key
            # scalar's actual bytes live.
            ptrs_seen = set()
            ptr_dumps = []
            for off in range(0, min(len(obj_dump) - 8, 4096), 8):
                cand = int.from_bytes(obj_dump[off:off+8], "little")
                if not looks_like_pointer(cand):
                    continue
                if cand in ptrs_seen:
                    continue
                # Heuristic: only chase pointers that map into heap (rw-p
                # in our typical Linux layout). Skip pointers into .text
                # (vtables) and pointers into .rodata.
                prov = memory_provenance(cand)
                if "[heap]" not in prov and "rw-p" not in prov and "[stack]" not in prov:
                    continue
                ptrs_seen.add(cand)
                target = read_bytes(cand, 256)
                if target is None:
                    continue
                # Skip if all-zero or all-one (uninteresting)
                if target == b"\x00" * 256 or target == b"\xff" * 256:
                    continue
                ptr_dumps.append((off, cand, target))

            log(f"  followed {len(ptr_dumps)} unique heap/stack ptrs in `this`'s first 4KB")
            ptr_path = f"{DUMP_DIR}/keyhunt-{_decrypt_hits}-pointers.txt"
            with open(ptr_path, "w") as f:
                for off, cand, target in ptr_dumps:
                    f.write(f"@this+{off:#06x}  → {cand:#x}  "
                            f"({memory_provenance(cand)})\n")
                    f.write(f"  256 bytes: {target.hex()}\n\n")
                f.flush()
            log(f"  → wrote pointer-chase report to {ptr_path}")

            return False  # let decryption proceed
        except Exception as e:
            log(f"  <Decrypt hook error: {e}>")
            return False


# --------------------------------------------------------------------------
# Mutation-panic safety net (mirrors safe-trace.py).
# Auto-kills the inferior on the first call to any device-mutating
# function. The key-hunter logic above lets ECIES decryption proceed
# so we can capture the key, but we still NEVER let any flash write
# reach the wire.
# --------------------------------------------------------------------------

PANIC_SYMBOLS = [
    # libdevices.so (RTS5409S_HID — upstream hub MCU)
    ("_ZN12RTS5409S_HID14reset_to_flashEv", "reset_to_flash — BOOTLOADER ENTER"),
    ("_ZN12RTS5409S_HID10reset_selfEv", "reset_self"),
    ("_ZN12RTS5409S_HID16erase_spare_bankEv", "erase_spare_bank"),
    ("_ZN12RTS5409S_HID15erase_tmp_flashEv", "erase_tmp_flash"),
    ("_ZN12RTS5409S_HID15write_hub_flashEjPKhh", "write_hub_flash"),
    ("_ZN12RTS5409S_HID15write_tmp_flashEjPKhh", "write_tmp_flash"),
    ("_ZN12RTS5409S_HID22write_hub_container_idERK11_hub_info_t", "write_hub_container_id"),
    ("_ZN12RTS5409S_HID19write_hub_serial_noERK11_hub_info_t", "write_hub_serial_no"),
    ("_ZN12RTS5409S_HID11update_selfERK8PROG_CFG", "update_self"),
    ("_ZN12RTS5409S_HID15update_hub_infoERK8PROG_CFG", "update_hub_info"),
    ("_ZN12RTS5409S_HID14secure_programERK15SECURE_PROG_CFG", "secure_program"),
    ("_ZN12RTS5409S_HID11tbt_programEhjPhh", "tbt_program"),
    ("_ZN12RTS5409S_HID19secure_control_gpioEhh", "secure_control_gpio"),
    # libhub.so (FL5500_IIC, Rts5409s_IIC)
    ("_ZN14FL5500_IIC_ISP3ispEv", "FL5500_IIC_ISP::isp — UPDATE ENTRY"),
    ("_ZN14FL5500_IIC_ISP10update_hubEv", "FL5500_IIC_ISP::update_hub"),
    ("_ZN14FL5500_IIC_API17sram_to_spi_flashEjPhii", "sram_to_spi_flash"),
    ("_ZN14FL5500_IIC_API16write_spi_sectorEPhi", "write_spi_sector"),
    ("_ZN14FL5500_IIC_API23trigger_write_spi_flashEv", "trigger_write_spi_flash"),
    ("_ZN14FL5500_IIC_API9reset_hubEv", "reset_hub"),
    ("_ZN16Rts5409s_IIC_ISP3ispEv", "Rts5409s_IIC_ISP::isp — UPDATE ENTRY"),
    ("_ZN16Rts5409s_IIC_ISP7programEv", "Rts5409s_IIC_ISP::program"),
    ("_ZN16Rts5409s_IIC_API11write_flashEPhh", "Rts5409s_IIC_API::write_flash"),
    ("_ZN16Rts5409s_IIC_API10soft_resetEv", "Rts5409s_IIC_API::soft_reset"),
]


class PanicHook(gdb.Breakpoint):
    def __init__(self, sym, label):
        super().__init__(sym, type=gdb.BP_BREAKPOINT, internal=False)
        self.label = label

    def stop(self):
        pc = int(gdb.parse_and_eval("(unsigned long long)$pc"))
        log("\n" + "!" * 60)
        log(f"!!! PANIC HOOK fired: {self.label}  @ {pc:#x}")
        try:
            bt = gdb.execute("bt 8", to_string=True)
            log("backtrace at panic:")
            for line in bt.splitlines()[:8]:
                log(f"  {line}")
        except gdb.error:
            pass
        log("!!! AUTO-KILLING inferior to prevent device write.")
        log("!" * 60)
        try:
            gdb.execute("kill")
        except gdb.error as e:
            log(f"  kill failed: {e}")
        try:
            gdb.execute("quit")
        except (gdb.error, SystemExit):
            pass
        return True


PRIVKEY_INIT_SYMBOLS = [
    # DL_PrivateKey_EC<ECP>::Initialize(group, Integer scalar)
    ("_ZN8CryptoPP16DL_PrivateKey_ECINS_3ECPEE10InitializeERKNS_21DL_GroupParameters_ECIS1_EERKNS_7IntegerE",
     "Initialize(group, scalar)"),
    # DL_PrivateKey_EC<ECP>::Initialize(group, Q, x, ?) — 5-arg overload
    ("_ZN8CryptoPP16DL_PrivateKey_ECINS_3ECPEE10InitializeERKS1_RKNS_8ECPPointERKNS_7IntegerESA_",
     "Initialize(group, Q, x, ?)"),
    # DL_PrivateKey_EC<ECP>::BERDecodePrivateKey(transformation, indef, len)
    ("_ZN8CryptoPP16DL_PrivateKey_ECINS_3ECPEE19BERDecodePrivateKeyERNS_22BufferedTransformationEbm",
     "BERDecodePrivateKey"),
    # DL_PrivateKey<ECPPoint>::AssignFrom(NameValuePairs)
    ("_ZN8CryptoPP13DL_PrivateKeyINS_8ECPPointEE10AssignFromERKNS_14NameValuePairsE",
     "AssignFrom(NVP)"),
]


gdb.execute("set breakpoint pending on")
CertifyDEntry()
DecryptHook()
for sym, label in PRIVKEY_INIT_SYMBOLS:
    try:
        PrivateKeyInitHook(sym, label)
    except Exception as e:
        log(f">>> failed to arm privkey hook on {sym}: {e}")
try:
    GunzipHook()
except Exception as e:
    log(f">>> failed to arm Gunzip hook: {e}")
for sym, label in PANIC_SYMBOLS:
    try:
        PanicHook(sym, label)
    except Exception as e:
        log(f">>> failed to arm panic hook on {sym}: {e}")
log(">>> key-hunter armed:")
log(">>>   • Certify::d entry (logs input size)")
log(">>>   • DL_DecryptorBase<ECPPoint>::Decrypt (dumps `this` + chases pointers)")
log(">>>   • DL_PrivateKey_EC::Initialize / BERDecodePrivateKey / AssignFrom")
log(">>>     (extracts the EC scalar by walking the Integer SecBlock)")
log(">>>   • Gunzip::ProcessDecompressedData (saves first 16 plaintext chunks)")
log(">>>   • Mutation panic chain: FIRST device-mutating call kills inferior.")
log(f">>> log   → {LOG_PATH}")
log(f">>> dumps → {DUMP_DIR}/keyhunt-*")
