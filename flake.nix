{
  description = "Dell U4025QW firmware update — reverse-engineering tooling";

  # Why two nixpkgs:
  #   stable    — everything except Ghidra (boring deps: python, gnumake, …).
  #   unstable  — Ghidra 12.x, which has materially better C++ analysis than
  #               25.11 stable's Ghidra 11.4.2 (RecoverClassesFromRTTIScript
  #               gets a steady stream of fixes upstream).
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, nixpkgs-unstable, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        unstable = import nixpkgs-unstable { inherit system; };

        # --- Ghidra C++ class analysis ---------------------------------
        # Goal: cleaner decompilation of Wistron's heavily-virtual-
        # inheritance class hierarchies (IIC_INTF / HID_INTF base ⇄
        # RTS5409S_HID / FL5500_IIC_API / … overrides). The built-in
        # RecoverClassesFromRTTIScript that ships with Ghidra explicitly
        # skips virtual-inheritance classes in its GCC path (docstring:
        # "Gcc class data types are only recovered for classes without
        # virtual inheritance"), which is most of what we care about.
        #
        # The standalone Ghidra-Cpp-Class-Analyzer extension specifically
        # addressed virtual inheritance, but the upstream
        # (astrelsky/Ghidra-Cpp-Class-Analyzer) was archived in October
        # 2023, with the upstream README pointing users at Ghidra's
        # built-in. The only fork with 2026 commits is Fancy2209's, and
        # at the current head (ffad578) it has 30 unresolved compile
        # errors against Ghidra 12.0.4 — they were mid-port to Ghidra 12
        # but didn't finish. Building it (./nix/cpp-class-analyzer.nix
        # which is kept around for the day a working fork appears or we
        # invest in patching) hits API-break compile errors.
        #
        # Pragmatic choice: ship the flake with plain unstable Ghidra 12.
        # Built-in RTTI recovery still gives us typed vtables for
        # non-virtual-inheritance classes (CryptoPP, std::, …) and
        # named vftable pointers for everything; it just doesn't apply
        # class structs to `this` params for our chip classes. When a
        # maintained extension fork lands we swap to
        # `ghidra.withExtensions (es: [ cppClassAnalyzer ])` here.
        ghidra = unstable.ghidra;

        # --- Wrapper scripts -------------------------------------------
        # These capture the exact incantations we use so nobody has to
        # remember `-process X -scriptPath Y -postScript Z` by heart.
        # Paths are relative to the repo root — scripts must be run
        # from there (or via `nix run` which sets PWD appropriately).

        projectDir = "ghidra/project";
        projectName = "DellMonitorRT";
        scriptsDir = "ghidra/scripts";
        decompDir = "ghidra/decomp";
        binariesDir = "extracted/usr/share/Dell/firmware/U4025QW";

        # `analyze <relative-path-to-binary>` — import + full analysis
        # + run RecoverClassesFromRTTIScript so vtable types and class
        # data types are populated in the project before later decompile
        # passes. Run this once per binary we care about.
        analyzeScript = pkgs.writeShellApplication {
          name = "analyze";
          runtimeInputs = [ ghidra ];
          text = ''
            if [[ $# -lt 1 ]]; then
              echo "usage: analyze <relative-binary-path>" >&2
              echo "       (e.g. analyze plugins/libdisplay.so)" >&2
              exit 2
            fi
            target="$1"
            mkdir -p "${projectDir}"
            exec ghidra-analyzeHeadless \
              "${projectDir}" "${projectName}" \
              -import "${binariesDir}/$target" \
              -scriptPath "${scriptsDir}" \
              -postScript RecoverClassesFromRTTIScript.java \
              -overwrite
          '';
        };

        # `decompile <binary-basename>` — re-decompile an already-
        # imported binary and dump every function to decomp/<name>.c.
        # Idempotent on the RTTI recovery pass (the script no-ops if
        # it's already been run on the program).
        #
        # Uses DumpDecompiled.java (not .py). Ghidra 12 dropped Jython
        # in favor of PyGhidra (CPython via JEP), which requires a
        # different launcher (`pyghidra`) not currently wrapped in
        # nixpkgs' ghidra-analyzeHeadless. The Java script is the
        # original anyway; the .py was a workaround for a bundle-
        # loading bug in a specific Ghidra 11.x version that no longer
        # applies.
        decompileScript = pkgs.writeShellApplication {
          name = "decompile";
          runtimeInputs = [ ghidra ];
          text = ''
            if [[ $# -lt 1 ]]; then
              echo "usage: decompile <binary-basename>" >&2
              echo "       (e.g. decompile libdisplay.so → ${decompDir}/libdisplay.c)" >&2
              exit 2
            fi
            target="$1"
            stem="''${target%.so}"
            mkdir -p "${decompDir}"
            exec ghidra-analyzeHeadless \
              "${projectDir}" "${projectName}" \
              -process "$target" \
              -scriptPath "${scriptsDir}" \
              -preScript RecoverClassesFromRTTIScript.java \
              -postScript DumpDecompiled.java "${decompDir}/$stem.c" \
              -noanalysis
          '';
        };
      in
      {
        packages = {
          inherit ghidra;
          analyze = analyzeScript;
          decompile = decompileScript;
          default = ghidra;
        };

        apps = {
          # `nix run .#ghidra` — raw GUI, for hand exploration.
          ghidra = {
            type = "app";
            program = "${ghidra}/bin/ghidra";
          };
          # `nix run .#ghidra-analyzeHeadless -- <ghidra args...>` —
          # raw headless escape hatch.
          ghidra-analyzeHeadless = {
            type = "app";
            program = "${ghidra}/bin/ghidra-analyzeHeadless";
          };
          # `nix run .#analyze -- plugins/libdisplay.so`
          analyze = {
            type = "app";
            program = "${analyzeScript}/bin/analyze";
          };
          # `nix run .#decompile -- libdisplay.so`
          decompile = {
            type = "app";
            program = "${decompileScript}/bin/decompile";
          };
        };

        devShells.default = pkgs.mkShell {
          name = "dell-u4025qw-fw";
          packages = [
            ghidra
            analyzeScript
            decompileScript
            (pkgs.python3.withPackages (ps: with ps; [
              # parse-upg.py + the other analysis scripts:
              pycryptodome
              cryptography
              # for inspecting / extracting from binaries:
              pyelftools
              # USB/HID work:
              hidapi
            ]))
            pkgs.gnumake
            pkgs.tshark # for pcap analysis
          ];
          shellHook = ''
            echo
            echo "============== dell-u4025qw-fw =============="
            echo " ghidra                   — GUI Ghidra 12 (no CCA extension yet)"
            echo " ghidra-analyzeHeadless   — raw headless"
            echo " analyze <bin>            — import + full analysis + RTTI recovery"
            echo " decompile <bin>          — re-decompile to ghidra/decomp/<name>.c"
            echo
            echo " Binaries live under: ${binariesDir}/"
            echo " Decomp output:       ghidra/decomp/"
            echo
          '';
        };
      });
}
