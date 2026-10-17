#pragma once

#include <string>

#include "spi_backend.h"

namespace openpilot::pandad::compatibility {

// Linux-only explicit device entry point. Creating this object never opens or
// configures hardware. P2 offline validation only links it, never calls open.
// spidev's synchronous ioctl has no portable kernel deadline/cancel primitive:
// the caller budget is cooperative between syscalls, not a hard realtime bound.
class LinuxSpiIo final : public SpiIo {
public:
  LinuxSpiIo() = default;
  ~LinuxSpiIo() override;
  LinuxSpiIo(const LinuxSpiIo &) = delete;
  LinuxSpiIo &operator=(const LinuxSpiIo &) = delete;
  Status open_device(const std::string &path, std::uint32_t speed_hz = 50000000);
  void close_device() noexcept;
  bool is_open() const { return fd_ >= 0; }
  Status begin(std::uint32_t timeout_ms) override;
  void end() noexcept override;
  Result exchange(const std::vector<std::uint8_t> &tx, std::uint32_t timeout_ms) override;

private:
  int fd_ = -1;
  bool leased_ = false;
};

}  // namespace openpilot::pandad::compatibility
