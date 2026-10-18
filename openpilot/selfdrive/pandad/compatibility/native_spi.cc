#include "native_spi.h"

#include <algorithm>
#include <cerrno>
#include <fcntl.h>
#include <limits>

#ifdef __linux__
#include <linux/spi/spidev.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
#endif

namespace openpilot::pandad::compatibility {
namespace {
Status error_status(int error) {
  if (error == ENODEV || error == ENXIO || error == EBADF) return Status::Disconnected;
  if (error == ETIMEDOUT) return Status::Timeout;
  if (error == EAGAIN || error == EWOULDBLOCK) return Status::Busy;
  return Status::IoError;
}
Result rejected(Status status) { return {status, 0, {}, true, false}; }
bool canonical_number(const std::string &value) {
  if (value.empty() || (value.size() > 1 && value.front() == '0') || value.size() > 10) return false;
  std::uint64_t number = 0;
  for (const char ch : value) {
    if (ch < '0' || ch > '9') return false;
    number = number * 10 + static_cast<unsigned>(ch - '0');
    if (number > std::numeric_limits<std::uint32_t>::max()) return false;
  }
  return true;
}
class LinuxSpiSyscalls final : public NativeSpiSyscalls {
public:
#ifdef __linux__
  NativeSpiCall open_device(const std::string &path, int flags) override { return result(::open(path.c_str(), flags)); }
  NativeSpiCall stat_device(int fd, NativeSpiNode &node) override {
    struct stat metadata{};
    auto call = result(::fstat(fd, &metadata));
    if (call.value == 0) node = {S_ISCHR(metadata.st_mode), static_cast<std::uint32_t>(::major(metadata.st_rdev)), static_cast<std::uint32_t>(::minor(metadata.st_rdev))};
    return call;
  }
  NativeSpiCall read_mode(int fd, std::uint32_t &mode) override { return result(::ioctl(fd, SPI_IOC_RD_MODE32, &mode)); }
  NativeSpiCall read_bits(int fd, std::uint8_t &bits) override { return result(::ioctl(fd, SPI_IOC_RD_BITS_PER_WORD, &bits)); }
  NativeSpiCall read_speed(int fd, std::uint32_t &speed) override { return result(::ioctl(fd, SPI_IOC_RD_MAX_SPEED_HZ, &speed)); }
  NativeSpiCall lock_device(int fd, bool acquire) override { return result(::flock(fd, acquire ? LOCK_EX | LOCK_NB : LOCK_UN)); }
  NativeSpiCall message(int fd, const std::uint8_t *tx, std::uint8_t *rx, std::size_t bytes) override {
    struct spi_ioc_transfer transfer{};
    transfer.tx_buf = reinterpret_cast<std::uintptr_t>(tx);
    transfer.rx_buf = reinterpret_cast<std::uintptr_t>(rx);
    transfer.len = static_cast<std::uint32_t>(bytes);
    // speed_hz/bits_per_word/delay/cs_change remain zero: retain the inspected
    // driver configuration and use one full-duplex CS assertion, no overrides.
    return result(::ioctl(fd, SPI_IOC_MESSAGE(1), &transfer));
  }
  NativeSpiCall close_device(int fd) override { return result(::close(fd)); }
private:
  static NativeSpiCall result(int value) { return {value, value < 0 ? errno : 0}; }
#else
  NativeSpiCall open_device(const std::string &, int) override { return unsupported(); }
  NativeSpiCall stat_device(int, NativeSpiNode &) override { return unsupported(); }
  NativeSpiCall read_mode(int, std::uint32_t &) override { return unsupported(); }
  NativeSpiCall read_bits(int, std::uint8_t &) override { return unsupported(); }
  NativeSpiCall read_speed(int, std::uint32_t &) override { return unsupported(); }
  NativeSpiCall lock_device(int, bool) override { return unsupported(); }
  NativeSpiCall message(int, const std::uint8_t *, std::uint8_t *, std::size_t) override { return unsupported(); }
  NativeSpiCall close_device(int) override { return unsupported(); }
private:
  static NativeSpiCall unsupported() { return {-1, ENOTSUP}; }
#endif
};
}  // namespace

std::shared_ptr<NativeSpiSyscalls> make_linux_spi_syscalls() { return std::make_shared<LinuxSpiSyscalls>(); }

bool operator==(const NativeSpiSelector &a, const NativeSpiSelector &b) {
  return a.path == b.path && a.identity_evidence_sha256 == b.identity_evidence_sha256 &&
    a.physical_identity_verified == b.physical_identity_verified && a.expected_major == b.expected_major &&
    a.expected_minor == b.expected_minor && a.expected_mode == b.expected_mode &&
    a.expected_bits_per_word == b.expected_bits_per_word && a.expected_max_speed_hz == b.expected_max_speed_hz;
}

bool valid_native_spi_selector(const NativeSpiSelector &selector) {
  constexpr const char *prefix = "/dev/spidev";
  constexpr std::size_t prefix_size = 11;
  if (selector.path.compare(0, prefix_size, prefix) != 0 || selector.path.size() > 32) return false;
  const auto dot = selector.path.find('.', prefix_size);
  if (dot == std::string::npos || !canonical_number(selector.path.substr(prefix_size, dot - prefix_size)) ||
      !canonical_number(selector.path.substr(dot + 1))) return false;
  if (!selector.physical_identity_verified || selector.identity_evidence_sha256.size() != 64 ||
      !std::all_of(selector.identity_evidence_sha256.begin(), selector.identity_evidence_sha256.end(),
                   [](char ch) { return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'); })) return false;
  return selector.expected_major && *selector.expected_major > 0 && selector.expected_minor &&
         selector.expected_mode && selector.expected_bits_per_word && *selector.expected_bits_per_word > 0 &&
         *selector.expected_bits_per_word <= 32 && selector.expected_max_speed_hz && *selector.expected_max_speed_hz > 0;
}

NativeSpiIo::~NativeSpiIo() { end(); close(); }
void NativeSpiIo::close() noexcept {
  if (fd_ >= 0) {
    // Never retry close: Linux may already have released the descriptor on EINTR.
    (void)calls_->close_device(fd_);
    fd_ = -1;
  }
}

Status NativeSpiIo::check_node_and_configuration() {
  if (cancelled_.load()) return Status::Cancelled;
  NativeSpiNode node;
  auto call = calls_->stat_device(fd_, node);
  if (call.value != 0) return error_status(call.error);
  if (cancelled_.load()) return Status::Cancelled;
  if (!node.character_device || node.major != *selector_.expected_major || node.minor != *selector_.expected_minor) return Status::Invalid;
  std::uint32_t mode = 0, speed = 0;
  std::uint8_t bits = 0;
  call = calls_->read_mode(fd_, mode);
  if (call.value != 0) return error_status(call.error);
  if (cancelled_.load()) return Status::Cancelled;
  call = calls_->read_bits(fd_, bits);
  if (call.value != 0) return error_status(call.error);
  if (cancelled_.load()) return Status::Cancelled;
  call = calls_->read_speed(fd_, speed);
  if (call.value != 0) return error_status(call.error);
  if (cancelled_.load()) return Status::Cancelled;
  return mode == *selector_.expected_mode && bits == *selector_.expected_bits_per_word &&
         speed == *selector_.expected_max_speed_hz ? Status::Ok : Status::Invalid;
}

Status NativeSpiIo::activate(const NativeSpiSelector &selector, NativeSpiActivation permit) {
  if (cancelled_.load()) return Status::Cancelled;
  if (permit != NativeSpiActivation::VerifiedProfile || !valid_native_spi_selector(selector) || !calls_ || activated_once_) return Status::Invalid;
  activated_once_ = true;
  selector_ = selector;
  const auto opened = calls_->open_device(selector.path, O_RDWR | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
  if (opened.value < 0) return error_status(opened.error);
  fd_ = opened.value;
  if (cancelled_.load()) { close(); return Status::Cancelled; }
  const auto locked = calls_->lock_device(fd_, true);
  if (locked.value != 0) { close(); return error_status(locked.error); }
  lease_ = true;
  const auto status = check_node_and_configuration();
  end();
  if (status != Status::Ok || poisoned_ || cancelled_.load()) {
    close();
    return cancelled_.load() ? Status::Cancelled : (poisoned_ ? Status::IoError : status);
  }
  return Status::Ok;
}

Status NativeSpiIo::begin(std::uint32_t timeout_ms) {
  if (cancelled_.load()) return Status::Cancelled;
  if (fd_ < 0 || poisoned_) return Status::Disconnected;
  if (lease_) return Status::Busy;
  if (timeout_ms == 0 || timeout_ms > kBackendSliceMs) return Status::Invalid;
  const auto start = clock_.now_us();
  auto previous = start;
  const auto budget = static_cast<std::uint64_t>(timeout_ms) * 1000;
  auto elapsed = [&](std::uint64_t &spent) {
    const auto now = clock_.now_us();
    if (now < previous) { poisoned_ = true; return false; }
    previous = now; spent = now - start; return true;
  };
  for (std::uint32_t attempt = 0; attempt < kNativeSpiLockAttempts; ++attempt) {
    if (cancelled_.load()) return Status::Cancelled;
    std::uint64_t spent = 0;
    if (!elapsed(spent)) return Status::IoError;
    if (spent >= budget) return Status::Timeout;
    const auto locked = calls_->lock_device(fd_, true);
    if (locked.value == 0) {
      lease_ = true;
      auto status = check_node_and_configuration();
      if (cancelled_.load()) status = Status::Cancelled;
      else if (!elapsed(spent)) status = Status::IoError;
      else if (spent >= budget) status = Status::Timeout;
      if (status != Status::Ok) {
        poisoned_ = true;
        end();
      }
      return status;
    }
    if (locked.error != EAGAIN && locked.error != EWOULDBLOCK) return error_status(locked.error);
    if (attempt + 1 < kNativeSpiLockAttempts) {
      if (!elapsed(spent)) return Status::IoError;
      if (spent >= budget) return Status::Timeout;
      clock_.wait_us(static_cast<std::uint32_t>(std::min<std::uint64_t>(kNativeSpiLockWaitUs, budget - spent)));
    }
  }
  return Status::Busy;
}

void NativeSpiIo::end() noexcept {
  if (lease_) {
    lease_ = false;
    if (calls_->lock_device(fd_, false).value != 0) { poisoned_ = true; close(); }
  }
}

Result NativeSpiIo::exchange(const std::vector<std::uint8_t> &tx, std::uint32_t timeout_ms) {
  if (cancelled_.load()) return rejected(Status::Cancelled);
  if (fd_ < 0 || poisoned_) return rejected(Status::Disconnected);
  if (!lease_ || tx.empty() || tx.size() > kSpiBufferBytes || timeout_ms == 0 || timeout_ms > kBackendSliceMs) return rejected(Status::Invalid);
  const auto started = clock_.now_us();
  std::vector<std::uint8_t> rx(tx.size());
  const auto call = calls_->message(fd_, tx.data(), rx.data(), tx.size());
  if (call.value < 0) { poisoned_ = true; return {error_status(call.error), 0, {}, false, true}; }
  if (static_cast<std::size_t>(call.value) > tx.size()) { poisoned_ = true; return {Status::IoError, 0, {}, false, true}; }
  rx.resize(static_cast<std::size_t>(call.value));
  auto status = rx.size() == tx.size() ? Status::Ok : Status::Short;
  const auto completed = clock_.now_us();
  if (cancelled_.load()) status = Status::Cancelled;
  else if (completed < started) status = Status::IoError;
  else if (completed - started >= static_cast<std::uint64_t>(timeout_ms) * 1000) status = Status::Timeout;
  if (status != Status::Ok) poisoned_ = true;
  return {status, rx.size(), std::move(rx), true, static_cast<std::size_t>(call.value) != tx.size()};
}

}  // namespace openpilot::pandad::compatibility
