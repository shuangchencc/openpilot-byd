#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <vector>

namespace openpilot::pandad::compatibility {

enum class Kind { ControlRead, ControlWrite, BulkRead, BulkWrite };
enum class Status {
  Ok, Short, Timeout, Cancelled, Disconnected, Stall, Busy, Invalid, IoError, Poisoned,
};
const char *status_name(Status status);

// Ingress fields stay wide until validation. Bulk endpoints include the USB
// direction bit (e.g. RX 0x81, TX 0x03); control endpoint is always zero.
struct Request {
  Kind kind = Kind::ControlRead;
  std::uint32_t endpoint = 0;
  std::uint32_t request = 0;
  std::uint32_t value = 0;
  std::uint32_t index = 0;
  std::size_t read_length = 0;
  std::vector<std::uint8_t> payload{};
};

struct Result {
  Status status = Status::Invalid;
  // Confirmed application bytes. If actual_known is false this is only a lower
  // bound: a later staged chunk may also have transferred an unknown amount.
  std::size_t actual = 0;
  std::vector<std::uint8_t> data{};
  bool actual_known = true;
  // A failed write may have changed the device even when its actual byte count
  // is zero/unknown. Callers must never interpret an error as proof of no TX.
  bool delivery_uncertain = false;
  // Filled by Transport, preserving the adapter status when a later deadline
  // or cancellation changes status. Direct backend callers leave these unset.
  Status backend_status = Status::Invalid;
  bool backend_attempted = false;
};

inline constexpr std::size_t kMaxControlBytes = 4096;
inline constexpr std::size_t kMaxBulkBytes = 16384;
inline constexpr std::uint32_t kMaxTransportTimeoutMs = 100;
inline constexpr std::uint32_t kBackendSliceMs = 5;
inline constexpr std::uint32_t kLockWaitMs = 10;
inline constexpr std::uint32_t kWaitQuantumUs = 1000;

bool is_read(Kind kind);
bool valid_request(const Request &request);

class Backend {
public:
  virtual ~Backend() = default;
  // Exactly one bounded transfer attempt. No internal replay/reset/recovery.
  // The owner guarantees exclusive use of this backend/handle. An adapter must
  // honor timeout_ms (1..5), retain partial counts, and never perform I/O in its
  // constructor. Transport cannot preempt an adapter that breaks this contract.
  virtual Result transact(const Request &request, std::uint32_t timeout_ms) = 0;
  // Permanent cooperative cancellation; must only set local atomic state.
  // No device calls, blocking work or handle destruction are allowed here.
  virtual void cancel() noexcept {}
};

class Clock {
public:
  virtual ~Clock() = default;
  virtual std::uint64_t now_us() = 0;
  virtual void wait_us(std::uint32_t duration_us) = 0;
};

// One session/handle, one in-flight call. Objects and their backend/clock must
// outlive all callers; neither a backend nor mutable parser may be shared with
// another Transport. This class does not enumerate/open/close physical devices.
class Transport {
public:
  Transport(Backend &backend, Clock &clock) : backend_(backend), clock_(clock) {}
  Transport(const Transport &) = delete;
  Transport &operator=(const Transport &) = delete;

  // Total candidate budget: 1..100ms, each backend attempt <=5ms, lock wait
  // <=10ms. Reads retry ONLY a known zero-byte timeout; a partial read returns
  // immediately. Writes always have exactly one attempt and are never replayed.
  // Unknown read consumption poisons the session: confirmed prefix bytes remain
  // available for diagnosis, but the stream cannot safely resume after a gap.
  Result perform(const Request &request, std::uint32_t timeout_ms = 100);
  // Permanent, session-local cancellation. A new instance is required to resume.
  // An already running bounded backend call is allowed to finish before return.
  void cancel() noexcept { cancelled_.store(true); backend_.cancel(); }
  bool healthy() const { return !cancelled_.load() && !poisoned_.load(); }

private:
  Backend &backend_;
  Clock &clock_;
  std::mutex mutex_;
  std::atomic<bool> cancelled_{false};
  std::atomic<bool> poisoned_{false};
};

}  // namespace openpilot::pandad::compatibility
