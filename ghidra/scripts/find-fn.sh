#!/usr/bin/env bash
# Pull a single function (and its signature) from the decompiled C dump.
# Usage:  find-fn.sh <decomp.c> <pattern>
#
# Each function in the dump is bracketed by `// ===== <name> @ <addr> =====`
# and a blank line at end. We awk between matching markers.
set -euo pipefail
file="$1"; shift
pattern="$1"; shift
awk -v pat="$pattern" '
  /^\/\/ ===== / {
    if (in_fn) { print ""; in_fn = 0 }
    if ($0 ~ pat) { in_fn = 1 }
  }
  in_fn { print }
  END { if (in_fn) print "" }
' "$file"
