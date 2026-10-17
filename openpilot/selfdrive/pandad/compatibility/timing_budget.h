#pragma once

#include <cstddef>
#include <cstdint>

namespace openpilot::pandad::compatibility {

// Candidate scheduler contract, pure policy only. P3/P4 must connect this to
// actual health collection, heartbeat scheduling and the group TX barrier.
// These constants neither authorize a command nor implement MCU liveness.
inline constexpr std::size_t kInflightCapacity = 1;
inline constexpr std::size_t kPendingQueueCapacity = 0;
inline constexpr std::uint64_t kHealthPeriodUs = 100000;
inline constexpr std::uint64_t kHealthMaxAgeUs = 250000;
inline constexpr std::uint64_t kHeartbeatPeriodUs = 100000;
inline constexpr std::uint64_t kHeartbeatMaxAgeUs = 250000;

// Unsigned subtraction tolerates a single wrapping clock. Intervals >=2^63
// (including future samples) are invalid, never made fresh by signed casts.
inline bool valid_elapsed(std::uint64_t now, std::uint64_t then) {
  return now - then < (std::uint64_t{1} << 63);
}
inline bool health_fresh(bool observed, std::uint64_t now, std::uint64_t received) {
  return observed && valid_elapsed(now, received) && now - received < kHealthMaxAgeUs;
}
inline bool heartbeat_due(bool sent, std::uint64_t now, std::uint64_t last_sent) {
  return !sent || !valid_elapsed(now, last_sent) || now - last_sent >= kHeartbeatPeriodUs;
}
inline bool heartbeat_overdue(bool sent, std::uint64_t now, std::uint64_t last_sent) {
  return !sent || !valid_elapsed(now, last_sent) || now - last_sent >= kHeartbeatMaxAgeUs;
}
inline bool queue_admissible(std::size_t inflight, std::size_t pending) {
  return inflight < kInflightCapacity && pending == kPendingQueueCapacity;
}

}  // namespace openpilot::pandad::compatibility
