#pragma once

#include <atomic>
#include <cstdint>
#include <vector>

#include "transport.h"

namespace openpilot::pandad::compatibility {

inline constexpr std::size_t kSpiBufferBytes = 4096;
inline constexpr std::size_t kSpiPayloadBytes = 4032;
inline constexpr std::size_t kSpiFirstReadBytes = 1013;
inline constexpr std::size_t kSpiRequestHeaderBytes = 9;
inline constexpr std::size_t kSpiResponseHeaderBytes = 7;
inline constexpr std::uint32_t kSpiPollWaitUs = 100;
inline constexpr std::uint32_t kSpiMaxPolls = 32;

// Full-duplex, one CS assertion per exchange. The response data and actual count
// refer to wire bytes, not application bytes. Implementations must not retry.
// begin/end protect a complete request/response from another process on the same
// physical bus. They do not serialize unrelated boards or USB handles.
class SpiIo {
public:
  virtual ~SpiIo() = default;
  virtual Status begin(std::uint32_t timeout_ms) { (void)timeout_ms; return Status::Ok; }
  virtual void end() noexcept {}
  // Local cooperative cancellation only; never close a concurrent syscall.
  virtual void cancel() noexcept {}
  virtual Result exchange(const std::vector<std::uint8_t> &tx, std::uint32_t timeout_ms) = 0;
};

// Inert until transact(). first_request_id is an explicit session nonce supplied
// by the future opener (randomized between sessions), not a hard-coded runtime
// default. IDs only advance for new requests; exhaustion fails before wrapping.
// Neither this adapter nor its owner may replay an uncertain request.
class SpiBackend final : public Backend {
public:
  SpiBackend(SpiIo &io, Clock &clock, std::uint32_t first_request_id)
      : io_(io), clock_(clock), next_id_(first_request_id) {}
  Result transact(const Request &request, std::uint32_t timeout_ms) override;
  void cancel() noexcept override { cancelled_.store(true); io_.cancel(); }

private:
  Result transfer(std::uint8_t endpoint, const std::vector<std::uint8_t> &tx,
                  std::size_t max_rx, bool write, std::uint64_t start_us,
                  std::uint64_t budget_us);
  Status boundary(std::uint64_t start_us, std::uint64_t budget_us);
  std::uint32_t remaining_ms(std::uint64_t start_us, std::uint64_t budget_us);

  SpiIo &io_;
  Clock &clock_;
  std::uint64_t next_id_;
  std::atomic<bool> cancelled_{false};
};

}  // namespace openpilot::pandad::compatibility
