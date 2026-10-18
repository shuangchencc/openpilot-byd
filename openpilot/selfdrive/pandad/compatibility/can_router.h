#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <optional>
#include <vector>

#include "can_codec.h"
#include "session_registry.h"

namespace openpilot::pandad::compatibility {

// Candidate offline budgets, not measured device timing guarantees. The pinned
// upstream sendcan allowance is 1s; P3 deliberately tightens it to 100ms.
inline constexpr std::uint64_t kSendcanMaxAgeUs = 100000;
inline constexpr std::uint64_t kTxQueueMaxAgeUs = 20000;
inline constexpr std::uint64_t kRxMaxAgeUs = 100000;
inline constexpr std::size_t kTxFramesPerSlot = 64;
inline constexpr std::size_t kRxFramesPerSlot = 256;
inline constexpr std::size_t kMaxTxBatchFrames = 64;

enum class RouterError {
  None, Empty, GroupUnavailable, InvalidArgument, InvalidEvent, InvalidRoute,
  Malformed, Expired, FutureTimestamp, StaleSession, Overflow, ClockRollback,
};
const char *router_error_name(RouterError error);

template <typename T>
struct RouterResult {
  T value{};
  RouterError error = RouterError::None;
  explicit operator bool() const { return error == RouterError::None; }
};

// Capture this before starting a read. Every callback must carry the original
// token. The router always compares it with its referenced live registry.
struct RouteToken {
  std::uint32_t slot = 0;
  std::uint64_t epoch = 0;
  std::uint64_t connection_token = 0;
  std::uint64_t group_generation = 0;
};

struct HostTxFrame {
  std::uint64_t address = 0;
  std::uint32_t source = 0;
  bool extended = false;
  bool fd = false;
  std::vector<std::uint8_t> payload;
};
struct TxBatch {
  bool valid = false;
  std::uint64_t source_timestamp_us = 0;
  std::vector<HostTxFrame> frames;
  // Capture when the event enters this session, before any reconnect. A caller
  // must not relabel a buffered old event with a newly observed generation.
  std::uint64_t group_generation = 0;
};

// A one-shot dequeue result for offline inspection. This is NOT a permission
// to transmit. There is deliberately no Transport reference or I/O method.
struct SimulatedTxIntent {
  RouteToken token;
  CanFrame frame;
  std::vector<std::uint8_t> encoded;
  std::uint64_t source_timestamp_us = 0;
  std::uint64_t enqueue_timestamp_us = 0;
  bool real_tx_authorized = false;
};
struct RoutedRxFrame {
  RouteToken token;
  CanFrame frame;
  std::uint32_t source = 0;
  // First fragment receive time, never refreshed at completion/publication.
  std::uint64_t receive_timestamp_us = 0;
};
// Immutable diagnostic evidence from a fully validated MCU rejection. It is
// never a TX intent, and survives queue retirement without becoming current RX.
struct RejectionEvidence {
  RouteToken token{};
  std::uint32_t source = 0;
  std::uint32_t local_bus = 0;
  std::uint64_t address = 0;
  std::uint64_t receive_timestamp_us = 0;
};
struct RouterCounters {
  std::uint64_t safety_rejections = 0;
  std::uint64_t invalid_batches = 0;
  std::uint64_t malformed = 0;
  std::uint64_t stale_callbacks = 0;
  std::uint64_t tx_overflows = 0;
  std::uint64_t rx_overflows = 0;
  std::uint64_t expired_tx = 0;
  std::uint64_t expired_rx = 0;
  std::uint64_t expired_fragments = 0;
  std::uint64_t epoch_tx_drops = 0;
  std::uint64_t epoch_rx_drops = 0;
  std::uint64_t epoch_fragment_bytes = 0;
  std::uint64_t clock_faults = 0;
};
struct RouterDepths {
  std::array<std::size_t, 2> tx{};
  std::array<std::size_t, 2> rx{};
  std::array<std::size_t, 2> fragment_bytes{};
};

// Single serialized event-loop contract. No internal threads, handles, device
// discovery, recovery or transmission. Registry mutations and router calls must
// be serialized by the owner. Only per-slot FIFO is defined, no board-to-board
// physical ordering. Any epoch/generation change clears ALL queues/fragments.
class CanRouter {
public:
  explicit CanRouter(SessionRegistry &registry);
  CanRouter(const CanRouter &) = delete;
  CanRouter &operator=(const CanRouter &) = delete;

  RouterResult<RouteToken> current_token(std::uint32_t slot, std::uint64_t now_us);
  RouterResult<std::size_t> enqueue_tx(const TxBatch &batch, std::uint64_t now_us);
  RouterResult<SimulatedTxIntent> take_tx(std::uint32_t slot, std::uint64_t now_us);
  // Revalidate a caller-held result just before a SIMULATED dispatch. This is
  // an inspection, not a live capability or a replay/consumption mechanism.
  RouterResult<bool> validate_intent(const SimulatedTxIntent &intent, std::uint64_t now_us);
  RouterResult<std::size_t> receive(const RouteToken &token, const std::uint8_t *bytes,
                                  std::size_t size, std::uint64_t receive_timestamp_us,
                                  std::uint64_t now_us);
  RouterResult<RoutedRxFrame> take_rx(std::uint32_t slot, std::uint64_t now_us);
  // Recheck caller-held data before a later simulated publication; the source
  // timestamp and original token must be preserved, including on republication.
  RouterResult<bool> validate_rx_frame(const RoutedRxFrame &frame, std::uint64_t now_us);
  RouterResult<RouterDepths> inspect(std::uint64_t now_us);
  // P4 barrier changes discard local work even if physical handles stay live.
  void discard_pending() { clear_all(); }
  // Ordinary noOutput/disabled operation suppresses TX, but still observes CAN.
  // Connection/control epoch changes and faults continue to clear all queues.
  void discard_tx_pending();
  const RouterCounters &counters() const { return counters_; }
  std::optional<RejectionEvidence> latest_rejection() const { return latest_rejection_; }

private:
  struct SlotState {
    explicit SlotState(CanCapability capability) : parser(capability) {}
    CanStreamParser parser;
    std::deque<SimulatedTxIntent> tx;
    std::deque<RoutedRxFrame> rx;
    std::optional<std::uint64_t> fragment_timestamp_us;
    std::optional<std::uint64_t> last_receive_timestamp_us;
  };
  void synchronize();
  void clear_all();
  RouterError prepare(std::uint64_t now_us);
  RouterError invalidate(RouterError error, const char *reason);
  bool current(const RouteToken &token) const;
  RouteToken token_for(std::size_t slot) const;
  void expire_queues(std::uint64_t now_us);

  SessionRegistry &registry_;
  std::array<SlotState, 2> slots_;
  std::uint64_t generation_ = 0;
  std::array<std::uint64_t, 2> epochs_{};
  std::array<std::uint64_t, 2> connections_{};
  bool group_available_ = false;
  bool initialized_ = false;
  bool clock_failed_ = false;
  std::optional<std::uint64_t> last_now_us_;
  RouterCounters counters_;
  std::optional<RejectionEvidence> latest_rejection_;
};

}  // namespace openpilot::pandad::compatibility
