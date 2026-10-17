#include "linux_spi_io.h"

#ifdef __linux__
#include <cerrno>
#include <fcntl.h>
#include <linux/spi/spidev.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <unistd.h>

namespace openpilot::pandad::compatibility {
namespace {
Status error_status(int error) {
  switch (error) {
    case ENODEV: case ENXIO: case ESHUTDOWN: return Status::Disconnected;
    case ETIMEDOUT: return Status::Timeout;
    case EBUSY: case EAGAIN: return Status::Busy;
    case ECANCELED: return Status::Cancelled;
    default: return Status::IoError;
  }
}
}  // namespace

LinuxSpiIo::~LinuxSpiIo() { close_device(); }

Status LinuxSpiIo::open_device(const std::string &path, std::uint32_t speed_hz) {
  if (fd_ >= 0 || path.empty() || speed_hz == 0 || speed_hz > 50000000) return Status::Invalid;
  fd_ = ::open(path.c_str(), O_RDWR | O_CLOEXEC);
  if (fd_ < 0) return error_status(errno);
  // Configuration is an explicit hardware action too. Respect an existing
  // user's bus lease before issuing any configuration ioctl.
  const auto lock_status = begin(kBackendSliceMs);
  if (lock_status != Status::Ok) {
    close_device();
    return lock_status;
  }
  std::uint32_t mode = SPI_MODE_0;
  std::uint8_t bits = 8;
  if (::ioctl(fd_, SPI_IOC_WR_MODE32, &mode) < 0 ||
      ::ioctl(fd_, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz) < 0 ||
      ::ioctl(fd_, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0) {
    const auto result = error_status(errno);
    close_device();
    return result;
  }
  end();
  return Status::Ok;
}

void LinuxSpiIo::close_device() noexcept {
  if (fd_ >= 0) {
    end();
    ::close(fd_);
    fd_ = -1;
  }
}

Status LinuxSpiIo::begin(std::uint32_t timeout_ms) {
  if (fd_ < 0) return Status::Disconnected;
  if (leased_ || timeout_ms == 0 || timeout_ms > kBackendSliceMs) return Status::Invalid;
  // One nonblocking attempt. No flock wait can hold up another board.
  if (::flock(fd_, LOCK_EX | LOCK_NB) < 0) return error_status(errno);
  leased_ = true;
  return Status::Ok;
}

void LinuxSpiIo::end() noexcept {
  if (leased_) {
    (void)::flock(fd_, LOCK_UN);
    leased_ = false;
  }
}

Result LinuxSpiIo::exchange(const std::vector<std::uint8_t> &tx, std::uint32_t timeout_ms) {
  if (fd_ < 0) return {Status::Disconnected, 0, {}, true, false};
  if (!leased_ || tx.empty() || tx.size() > kSpiBufferBytes || timeout_ms == 0 || timeout_ms > kBackendSliceMs) {
    return {Status::Invalid, 0, {}, true, false};
  }
  std::vector<std::uint8_t> rx(tx.size(), 0);
  spi_ioc_transfer transfer{};
  transfer.tx_buf = reinterpret_cast<std::uintptr_t>(tx.data());
  transfer.rx_buf = reinterpret_cast<std::uintptr_t>(rx.data());
  transfer.len = static_cast<std::uint32_t>(tx.size());
  // Never retry EINTR or timeout: the device may have handled the request.
  const int count = ::ioctl(fd_, SPI_IOC_MESSAGE(1), &transfer);
  if (count < 0) return {error_status(errno), 0, {}, false, true};
  if (static_cast<std::size_t>(count) > tx.size()) return {Status::IoError, 0, {}, false, true};
  rx.resize(static_cast<std::size_t>(count));
  return {rx.size() == tx.size() ? Status::Ok : Status::Short, rx.size(), std::move(rx), true, false};
}

}  // namespace openpilot::pandad::compatibility
#else
#error "linux_spi_io.cc must only be compiled for Linux; pure SPI tests use SpiIo fakes"
#endif
