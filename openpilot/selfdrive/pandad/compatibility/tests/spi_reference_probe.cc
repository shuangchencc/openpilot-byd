// Independent outbound wire oracle. Use the actual pinned original host header
// and Panda control packet, with zlib rather than the candidate CRC routine.
// This file never constructs PandaSpiHandle and cannot access any device.
#include <atomic>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>
#include <zlib.h>

#define private public
#include "selfdrive/pandad/panda_comms.h"
#undef private
#include "panda/board/comms_definitions.h"

using Bytes = std::vector<std::uint8_t>;
static_assert(sizeof(PandaSpiHandle::spi_header) == 9);
static_assert(sizeof(PandaSpiHandle::spi_resp_header) == 7);
static_assert(sizeof(ControlPacket_t) == 7);
static_assert(SPI_BUF_SIZE == 4096);

namespace {
Bytes make(std::uint8_t endpoint, const Bytes &payload, std::uint16_t receive) {
  PandaSpiHandle::spi_header header{endpoint, 0x12345678, static_cast<std::uint16_t>(payload.size()),
                                    static_cast<std::uint16_t>(receive + sizeof(PandaSpiHandle::spi_resp_header) + 4)};
  Bytes bytes(sizeof(header));
  std::memcpy(bytes.data(), &header, sizeof(header));
  bytes.insert(bytes.end(), payload.begin(), payload.end());
  const auto checksum = static_cast<std::uint32_t>(::crc32(0, bytes.data(), static_cast<unsigned>(bytes.size())));
  const auto length = bytes.size();
  bytes.resize(length + sizeof(checksum));
  std::memcpy(bytes.data() + length, &checksum, sizeof(checksum));
  return bytes;
}
Bytes control(bool reading) {
  ControlPacket_t packet{static_cast<std::uint8_t>(reading ? 0xdd : 0xdc), 0x1234, 0xabce,
                         static_cast<std::uint16_t>(reading ? 8 : 0)};
  Bytes data(sizeof(packet));
  std::memcpy(data.data(), &packet, sizeof(packet));
  return data;
}
void print(const char *name, const Bytes &bytes) {
  std::cout << name << ':' << std::hex << std::setfill('0');
  for (const auto byte : bytes) std::cout << std::setw(2) << static_cast<unsigned>(byte);
  std::cout << '\n';
}
}  // namespace

int main() {
  const std::uint32_t endian = 1;
  if (*reinterpret_cast<const std::uint8_t *>(&endian) != 1) return 2;
  print("control_read", make(0, control(true), 8));
  print("control_write", make(0, control(false), 0));
  print("bulk_write", make(3, {1, 2, 3}, 0));
  print("bulk_read", make(0x81, {}, 16));
  print("first_large_read", make(0x81, {}, 1024 - sizeof(PandaSpiHandle::spi_resp_header) - 4));
}
