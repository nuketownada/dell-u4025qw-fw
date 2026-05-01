// decrypt-bin.cpp
// Like decrypt-blob.cpp but takes RAW binary input (no Base64URL outer).
// Tests whether the same DataDecryptorWithMAC scheme used by Dell's
// decrypt_string() (passphrase = "Wistron@<MODEL>") applies to the .upg
// binary firmware blobs too.
//
// Usage:
//   decrypt-bin <passphrase> <path/to/binfile> [out.bin]
//
// Build:
//   nix-shell -p cryptopp gcc --run \
//     'g++ -std=c++17 -O0 -o decrypt-bin decrypt-bin.cpp -lcryptopp'

#include <cryptopp/aes.h>
#include <cryptopp/default.h>
#include <cryptopp/filters.h>
#include <cryptopp/files.h>
#include <cryptopp/hmac.h>
#include <cryptopp/sha.h>

#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

using namespace CryptoPP;

typedef DataDecryptorWithMAC<
    Rijndael, SHA256, HMAC<SHA256>,
    DataParametersInfo<16, 16, 32, 8, 2500>>
    DellDecryptor;

int main(int argc, char **argv) {
  if (argc < 3) {
    std::cerr << "usage: " << argv[0] << " <passphrase> <bin-input> [out.bin]\n";
    return 2;
  }
  std::string passphrase = argv[1];
  std::string in_path = argv[2];
  std::string out_path = (argc >= 4) ? argv[3] : "/dev/stdout";

  std::ifstream in(in_path, std::ios::binary);
  if (!in) { std::cerr << "cannot open " << in_path << "\n"; return 1; }
  std::stringstream buf;
  buf << in.rdbuf();
  std::string raw = buf.str();
  std::cerr << "input " << raw.size() << " bytes\n";

  std::string pt;
  try {
    StringSource src(
        raw, true,
        new DellDecryptor(
            passphrase.c_str(),
            new StringSink(pt)));
    std::cerr << "decrypted: " << pt.size() << " bytes\n";
    if (out_path == "/dev/stdout") {
      std::cout.write(pt.data(), pt.size());
    } else {
      std::ofstream out(out_path, std::ios::binary);
      out.write(pt.data(), pt.size());
      std::cerr << "wrote → " << out_path << "\n";
    }
    return 0;
  } catch (const Exception &e) {
    std::cerr << "decrypt failed: " << e.what() << "\n";
    return 1;
  }
}
