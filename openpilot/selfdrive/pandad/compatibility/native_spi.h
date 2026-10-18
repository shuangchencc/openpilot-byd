#pragma once

#include <atomic>
#include <memory>
#include <optional>
#include <string>

#include "spi_backend.h"

namespace openpilot::pandad::compatibility {

// These are verified observations of this exact node and board. Missing identity
// or configuration is unbound; no path, MCU, mode or speed is guessed.
struct NativeSpiSelector {
  std::string path;
  std::string identity_evidence_sha256;
  bool physical_identity_verified = false;
  std::optional<std::uint32_t> expected_major;
  std::optional<std::uint32_t> expected_minor;
  std::optional<std::uint32_t> expected_mode;
  std::optional<std::uint8_t> expected_bits_per_word;
  std::optional<std::uint32_t> expected_max_speed_hz;
};
bool operator==(const NativeSpiSelector &, const NativeSpiSelector &);
enum class NativeSpiActivation { Denied, VerifiedProfile };
struct NativeSpiCall { int value = -1; int error = 0; };
struct NativeSpiNode {
  bool character_device = false;
  std::uint32_t major = 0, minor = 0;
};

// Every syscall has an injected seam. The Linux implementation issues only
// SPI_IOC_RD_* and one SPI_IOC_MESSAGE(1), never WR_* or reset/GPIO operations.
// Implementations return the captured errno, not an ambient mutable errno.
class NativeSpiSyscalls {
public:
  virtual ~NativeSpiSyscalls() = default;
  virtual NativeSpiCall open_device(const std::string &, int flags) = 0;
  virtual NativeSpiCall stat_device(int fd, NativeSpiNode &) = 0;
  virtual NativeSpiCall read_mode(int fd, std::uint32_t &) = 0;
  virtual NativeSpiCall read_bits(int fd, std::uint8_t &) = 0;
  virtual NativeSpiCall read_speed(int fd, std::uint32_t &) = 0;
  virtual NativeSpiCall lock_device(int fd, bool acquire) = 0;
  virtual NativeSpiCall message(int fd, const std::uint8_t *tx, std::uint8_t *rx, std::size_t bytes) = 0;
  virtual NativeSpiCall close_device(int fd) = 0;
};

// Construction is inert, including on non-Linux hosts. Unsupported syscall
// implementations fail closed; no device enumeration or fallback is performed.
std::shared_ptr<NativeSpiSyscalls> make_linux_spi_syscalls();
bool valid_native_spi_selector(const NativeSpiSelector &selector);
inline constexpr bool kNativeSpiKernelDeadlineProven = false;
inline constexpr std::uint32_t kNativeSpiLockAttempts = 32;
inline constexpr std::uint32_t kNativeSpiLockWaitUs = 100;

// The owner serializes activate/begin/exchange/end/destruction. Only cancel may
// run concurrently. cancel does not close or interrupt an in-flight kernel call.
// Linux synchronous SPI_IOC_MESSAGE has no hard timeout: offline budgets and
// cancellation are checked before/after it, not proof of a kernel deadline.
class NativeSpiIo final : public SpiIo {
public:
  NativeSpiIo(std::shared_ptr<NativeSpiSyscalls> calls, Clock &clock)
      : calls_(std::move(calls)), clock_(clock) {}
  ~NativeSpiIo() override;
  NativeSpiIo(const NativeSpiIo &) = delete;
  NativeSpiIo &operator=(const NativeSpiIo &) = delete;
  Status activate(const NativeSpiSelector &, NativeSpiActivation permit = NativeSpiActivation::Denied);
  Status begin(std::uint32_t timeout_ms) override;
  void end() noexcept override;
  void cancel() noexcept override { cancelled_.store(true); }
  Result exchange(const std::vector<std::uint8_t> &, std::uint32_t timeout_ms) override;
  bool active() const { return fd_ >= 0 && !cancelled_.load() && !poisoned_; }
private:
  Status check_node_and_configuration();
  void close() noexcept;
  std::shared_ptr<NativeSpiSyscalls> calls_;
  Clock &clock_;
  NativeSpiSelector selector_;
  int fd_ = -1;
  bool lease_ = false, activated_once_ = false, poisoned_ = false;
  std::atomic<bool> cancelled_{false};
};

}  // namespace openpilot::pandad::compatibility
