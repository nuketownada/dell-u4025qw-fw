# Ghidra headless post-script: decompile every function in the current
# program and write the C source to a file path passed via DUMP_OUT.
# Equivalent to DumpDecompiled.java but lighter to edit. Written for
# Jython (Ghidra's bundled Python 2.7-compatible interpreter).
# @category Decompilation

import os
from ghidra.app.decompiler import DecompInterface

decomp = DecompInterface()
decomp.openProgram(currentProgram)

out_path = os.environ.get("DUMP_OUT", "/tmp/decomp.c")
total = ok = fail = 0
with open(out_path, "w") as out:
    out.write("// Decompiled by DumpDecompiled.py\n")
    out.write("// Program: %s\n\n" % currentProgram.getName())
    fi = currentProgram.getFunctionManager().getFunctions(True)
    for f in fi:
        if monitor.isCancelled():
            break
        total += 1
        if f.isExternal() or f.isThunk():
            continue
        try:
            r = decomp.decompileFunction(f, 60, monitor)
            if r and r.decompileCompleted():
                out.write("// ===== %s @ %s =====\n" % (f.getName(True), f.getEntryPoint()))
                out.write("// signature: %s\n\n" % f.getSignature())
                out.write(r.getDecompiledFunction().getC())
                out.write("\n")
                ok += 1
            else:
                msg = "(null)" if not r else r.getErrorMessage()
                out.write("// ===== %s @ %s  (FAILED: %s) =====\n\n" %
                          (f.getName(True), f.getEntryPoint(), msg))
                fail += 1
        except Exception as e:
            out.write("// ===== %s  (EXCEPTION: %s) =====\n\n" % (f.getName(), e))
            fail += 1
        if total % 100 == 0:
            print("  ... %d functions processed" % total)
print("done: %d ok, %d failed (out of %d total)" % (ok, fail, total))
