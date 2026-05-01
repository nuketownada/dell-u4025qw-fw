// decrypt-blob.cpp
// Mirrors Dell's decrypt_string():
//   DataDecryptorWithMAC<Rijndael, SHA256, HMAC<SHA256>,
//                        DataParametersInfo<16, 16, 32, 8, 2500>>
// piped through Base64URLDecoder.
//
// Usage:
//   decrypt-blob <passphrase> <base64url-input>
//   decrypt-blob -                              # read pairs from stdin: each
//                                                line is <passphrase>:<b64url>
//
// Build:
//   nix-shell -p cryptopp gcc --run \
//     'g++ -std=c++17 -O0 -o decrypt-blob decrypt-blob.cpp -lcryptopp'
//

#include <cryptopp/aes.h>
#include <cryptopp/base64.h>
#include <cryptopp/default.h>
#include <cryptopp/filters.h>
#include <cryptopp/hex.h>
#include <cryptopp/hmac.h>
#include <cryptopp/sha.h>

#include <iostream>
#include <sstream>
#include <string>

using namespace CryptoPP;

// Match Dell's template instantiation exactly.
typedef DataDecryptorWithMAC<
    Rijndael, SHA256, HMAC<SHA256>,
    DataParametersInfo<16, 16, 32, 8, 2500>>
    DellDecryptor;

static bool decrypt_one(const std::string &passphrase,
                        const std::string &b64url, std::string &out_pt,
                        std::string &out_err) {
  try {
    // Base64URL decode → raw ciphertext.
    std::string raw;
    StringSource b64(
        b64url, true,
        new Base64URLDecoder(new StringSink(raw)));

    // DataDecryptorWithMAC eats the raw bytes and emits plaintext.
    StringSource src(
        raw, true,
        new DellDecryptor(
            passphrase.c_str(),
            new StringSink(out_pt)));
    return true;
  } catch (const Exception &e) {
    out_err = e.what();
    return false;
  } catch (const std::exception &e) {
    out_err = e.what();
    return false;
  }
}

int main(int argc, char **argv) {
  if (argc < 2) {
    std::cerr
        << "usage: decrypt-blob <passphrase> <b64url>\n"
        << "       decrypt-blob -    # read 'passphrase:b64url' lines from stdin\n";
    return 2;
  }

  if (std::string(argv[1]) == "-") {
    std::string line;
    while (std::getline(std::cin, line)) {
      if (line.empty() || line[0] == '#') continue;
      auto sep = line.find(':');
      if (sep == std::string::npos) {
        std::cerr << "bad line (no ':'): " << line << "\n";
        continue;
      }
      std::string passphrase = line.substr(0, sep);
      std::string b64 = line.substr(sep + 1);
      std::string pt, err;
      if (decrypt_one(passphrase, b64, pt, err)) {
        std::cout << "OK  " << b64 << "\n    → " << pt << "\n";
      } else {
        std::cout << "ERR " << b64 << "\n    → " << err << "\n";
      }
    }
    return 0;
  }

  if (argc < 3) {
    std::cerr << "usage: decrypt-blob <passphrase> <b64url>\n";
    return 2;
  }
  std::string passphrase = argv[1];
  std::string b64 = argv[2];
  std::string pt, err;
  if (decrypt_one(passphrase, b64, pt, err)) {
    std::cout << pt << "\n";
    return 0;
  }
  std::cerr << "decrypt failed: " << err << "\n";
  return 1;
}
