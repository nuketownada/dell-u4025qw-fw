// ecies-decrypt.cpp
//
// Decrypts a Dell U4025QW .upg binary firmware blob using the static
// secp521r1 ECC private key embedded in libhub.so as `CK_PV_RawData`.
//
// Pipeline (as observed in libhub.so Certify::d at offset 0x80990):
//
//   ECIES.Decrypt(blob, key) → plaintext_bytes
//   plaintext_bytes → Gunzip → HexDecoder → final firmware binary
//
// ECIES suite parameters (template args found in the same function):
//   - Curve: secp521r1 (DL_GroupParameters_EC<ECP>)
//   - KeyAgreement: DL_KeyAgreementAlgorithm_DH<ECPPoint, IncompatibleCofactorMultiplication>
//   - KDF: DL_KeyDerivationAlgorithm_P1363<ECPPoint, /*DHAES_MODE=*/true, P1363_KDF2<SHA3_512>>
//   - SymmetricEnc: DL_EncryptionAlgorithm_Xor<HMAC<SHA3_512>, /*DHAES_MODE=*/true, /*LABEL_OCTETS=*/false>
//
// Usage:
//   ecies-decrypt <key.pkcs8.der> <encrypted.bin> [output.bin]
//
// Build:
//   nix-shell -p cryptopp gcc --run \
//     'g++ -std=c++17 -O0 -o ecies-decrypt ecies-decrypt.cpp -lcryptopp'

#include <cryptopp/eccrypto.h>
#include <cryptopp/ecp.h>
#include <cryptopp/oids.h>
#include <cryptopp/sha3.h>
#include <cryptopp/hmac.h>
#include <cryptopp/files.h>
#include <cryptopp/filters.h>
#include <cryptopp/hex.h>
#include <cryptopp/gzip.h>
#include <cryptopp/osrng.h>
#include <cryptopp/pubkey.h>
#include <cryptopp/queue.h>

#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

using namespace CryptoPP;

// The exact ECIES variant Dell uses (matches the template parameters
// pulled out of the libhub.so decompile of Certify::d):
typedef ECIES<ECP, SHA3_512, IncompatibleCofactorMultiplication,
              /*DHAES_MODE=*/true, /*LABEL_OCTETS=*/false>
    DellECIES;

int main(int argc, char **argv) {
  if (argc < 3) {
    std::cerr << "usage: " << argv[0]
              << " <key.pkcs8.der> <encrypted.bin> [out.bin]\n";
    return 2;
  }
  const std::string key_path = argv[1];
  const std::string in_path = argv[2];
  const std::string out_path = (argc >= 4) ? argv[3] : "/dev/stdout";

  // Load the PKCS#8 DER private key.
  ByteQueue key_queue;
  FileSource(key_path.c_str(), true, new Redirector(key_queue));

  DellECIES::Decryptor decryptor;
  decryptor.AccessMaterial().Load(key_queue);
  std::cerr << "loaded private key from " << key_path << " (secp521r1)\n";

  // Read the encrypted blob.
  std::ifstream in(in_path, std::ios::binary);
  if (!in) { std::cerr << "cannot open " << in_path << "\n"; return 1; }
  std::stringstream buf;
  buf << in.rdbuf();
  const std::string ct = buf.str();
  std::cerr << "ciphertext: " << ct.size() << " bytes\n";

  // ECIES decrypt → SecByteBlock plaintext_bytes.
  AutoSeededRandomPool prng;
  prng.IncorporateEntropy(reinterpret_cast<const byte *>("seed"), 4);
  size_t pt_len = decryptor.MaxPlaintextLength(ct.size());
  if (pt_len == 0) {
    std::cerr << "MaxPlaintextLength = 0 (ciphertext too short)\n";
    return 1;
  }
  SecByteBlock pt(pt_len);
  DecodingResult res = decryptor.Decrypt(
      prng,
      reinterpret_cast<const byte *>(ct.data()), ct.size(),
      pt.BytePtr());
  if (!res.isValidCoding) {
    std::cerr << "ECIES decryption FAILED (MAC check or padding error)\n";
    return 1;
  }
  std::cerr << "ECIES plaintext: " << res.messageLength
            << " bytes (max " << pt_len << ")\n";

  // Gunzip → HexDecoder → final binary
  std::string final_bin;
  StringSource(
      pt.BytePtr(), res.messageLength, true,
      new Gunzip(
          new HexDecoder(new StringSink(final_bin))));
  std::cerr << "after Gunzip+HexDecode: " << final_bin.size() << " bytes\n";

  if (out_path == "/dev/stdout") {
    std::cout.write(final_bin.data(), final_bin.size());
  } else {
    std::ofstream out(out_path, std::ios::binary);
    out.write(final_bin.data(), final_bin.size());
    std::cerr << "wrote → " << out_path << "\n";
  }
  return 0;
}
