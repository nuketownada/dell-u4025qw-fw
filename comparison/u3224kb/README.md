# U3224KB cross-comparison

Quick fetch + check to confirm the ECIES decryption key in
`libhub.so` is **fleet-wide** (same key across multiple Dell
monitor models / multiple firmware build dates), not per-monitor
or per-release.

## Bundle

- **Model:** Dell U3224KB (32" 6K USB-C hub display, ~2023)
- **Firmware version:** M2T105 (Ubuntu)
- **Build date:** Dec 12, 2023 (per `libhub.so` mtime in the .deb)
- **Direct download:**
  https://dl.dell.com/FOLDER11069035M/1/Dell_U3224KB_FWUpdate_M2T105_Ubuntu.deb
  (the `.deb` itself is `.gitignore`d in this repo — re-fetch as needed)

## Extract

```sh
nix-shell -p dpkg --run 'dpkg -x Dell_U3224KB_FWUpdate_M2T105_Ubuntu.deb extracted/'
ls extracted/usr/share/Dell/firmware/U3224KB/plugins/libhub.so
```

## Verify the key matches U4025QW

The U3224KB `libhub.so` is **stripped** (no symbols), so the
`_ZL13CK_PV_RawData` symbol that pinpoints the key in the
U4025QW build is gone. But the PKCS#8 ASN.1 prefix for a
secp521r1 ECPrivateKey is fixed-format — locate by literal byte
search:

```python
import sys
data = open(sys.argv[1], "rb").read()
needle = bytes.fromhex(
    "3060020100301006072a8648ce3d020106052b81040023044930470201010442"
)
off = data.find(needle)
print(f"key at offset {off:#x}")
print(data[off:off+98].hex())
```

For the M2T105 build the key sits at offset `0x1805a0` and is
byte-identical to the U4025QW M3T105 key:

```
SHA-256: 0eede35b9795ae9464982485c118c82d4920e759814018bf3528ff48892c7bd0
```

Two different monitors, ~2 years apart in build dates, same
exact 98-byte PKCS#8 blob → strong evidence of one master keypair
across the whole Wistron-built Dell monitor fleet.
