{ lib, fetchFromGitHub, buildGhidraExtension }:

# Ghidra C++ Class and RTTI Analyzer — Fancy2209's fork.
#
# Background: the original astrelsky/Ghidra-Cpp-Class-Analyzer was archived
# in October 2023 and the upstream README points users at Ghidra's
# built-in RecoverClassesFromRTTIScript as the successor. In practice
# that built-in script's GCC class recovery is still labeled "early
# stages of development" in its own docstring and SKIPS classes with
# virtual inheritance — which is most non-trivial C++ class hierarchies.
# Our Wistron binaries (libdisplay.so, libdevices.so, libhub.so, …)
# rely on virtual inheritance heavily (IIC_INTF / HID_INTF base ⇄
# RTS5409S_HID / FL5500_IIC_API / … overrides), so the built-in is
# insufficient and we need the standalone extension.
#
# Fancy2209's fork is the only fork with commits in 2026, targeting
# Ghidra 12.x. The base code is unchanged from astrelsky's; it just
# follows Ghidra's evolving extension API.
buildGhidraExtension (finalAttrs: {
  pname = "Ghidra-Cpp-Class-Analyzer";
  version = "unstable-2026-01-29";

  src = fetchFromGitHub {
    owner = "Fancy2209";
    repo = "Ghidra-Cpp-Class-Analyzer";
    rev = "ffad578951ed92b196d5a32681151a8ed7d39501";
    hash = "sha256-uVl+7kHKOA5boXysAJ/KmV5EMndJVmDF4mviVWDVpEs=";
  };

  meta = {
    description =
      "Ghidra C++ class hierarchy and RTTI recovery (Fancy2209 fork, Ghidra 12 compatible)";
    homepage = "https://github.com/Fancy2209/Ghidra-Cpp-Class-Analyzer";
    license = lib.licenses.asl20;
  };
})
