#pragma once

#include <memory>
#include "runtime_integration.h"

namespace openpilot::pandad::compatibility {

enum class DiscoveryTaskKind { Discover, Reprobe };
struct DiscoveryTaskRequest {
  std::uint64_t generation = 0, started_us = 0, deadline_us = 0;
  DiscoveryTaskKind kind = DiscoveryTaskKind::Discover;
  DiscoverySnapshot previous;
};
// Ownership transfer only. Destruction must enqueue native cleanup, never join
// a worker or synchronously wait for kernel I/O on the owner thread.
struct DiscoveryTaskPayload { virtual ~DiscoveryTaskPayload() = default; };
struct DiscoveryTaskResult {
  std::uint64_t generation = 0;
  DiscoveryTaskKind kind = DiscoveryTaskKind::Discover;
  DiscoverySnapshot snapshot;
  // Fixed profile slot order, independently of snapshot.items order. These are
  // actual completed identity reads, never scheduler/handoff timestamps.
  std::array<std::uint64_t,2> slot_observed_us{};
  std::unique_ptr<DiscoveryTaskPayload> payload;
};
enum class DiscoveryPollState { Pending, Complete, Failed };
struct DiscoveryPollResult {
  DiscoveryPollState state = DiscoveryPollState::Pending;
  DiscoveryTaskResult result;
};
class DiscoveryTask {
public:
  virtual ~DiscoveryTask() = default;
  // Nonblocking mailbox query. The worker does enumeration, probes and cleanup;
  // it may not publish a lease directly or mutate the serialized runtime owner.
  virtual DiscoveryPollResult poll() = 0;
  virtual void cancel() noexcept = 0;  // local cancellation signal only
};
class DiscoveryTaskProvider {
public:
  virtual ~DiscoveryTaskProvider() = default;
  // Nonblocking enqueue. One outstanding task per scheduler, no unbounded
  // worker queue. A cancelled kernel call must retain its lane until it ends;
  // start returns null while that lane is unavailable.
  virtual std::unique_ptr<DiscoveryTask> start(const DiscoveryTaskRequest &) = 0;
};
class DiscoveryCommitSink {
public:
  virtual ~DiscoveryCommitSink() = default;
  // Transfer already probed retained leases for Discover; Reprobe only updates
  // same-handle evidence. Neither path reopens, claims, probes or performs I/O.
  virtual bool commit(const DiscoveryTaskRequest &, DiscoveryTaskResult &) = 0;
  // Revoke BOTH runtime epochs before releasing owners. No blocking cleanup.
  virtual void retire(const char *reason) noexcept = 0;
};
struct DiscoveryScheduleConfig {
  bool allow_scheduling = false;
  std::uint64_t reprobe_interval_us = 50000;
  std::uint64_t task_timeout_us = 100000;
  std::uint64_t max_observation_age_us = kLoopMaxGapUs;
  std::uint64_t initial_backoff_us = 10000, maximum_backoff_us = 500000;
  std::uint64_t callback_budget_us = 1000;
};
enum class DiscoveryScheduleState { Denied, Waiting, Discovering, Ready, Reprobing, Backoff, Stopped, Failed };
struct DiscoveryTickResult {
  DiscoveryScheduleState state = DiscoveryScheduleState::Denied;
  bool started = false, polled = false, committed = false, retired = false;
  std::string reason = "UNBOUND_OR_SCHEDULING_DENIED";
};

// Serialized owner-thread state machine. Tick does at most one start OR poll
// and never waits/sleeps. Health/heartbeat remain the caller's responsibility
// each cycle, even while a discovery task is pending. Callback timing is only
// checked after return; native hard-deadline proof is a separate production
// gate. Supplying an asynchronous interface is not proof of such a deadline.
class DiscoveryScheduler final : public DiscoveryProvider {
public:
  DiscoveryScheduler(const SessionProfile &, Clock &, DiscoveryTaskProvider &, DiscoveryCommitSink &,
                     DiscoveryScheduleConfig = {});
  ~DiscoveryScheduler();
  DiscoveryScheduler(const DiscoveryScheduler &)=delete;
  DiscoveryScheduler &operator=(const DiscoveryScheduler &)=delete;
  DiscoveryScheduler(DiscoveryScheduler &&)=delete;
  DiscoveryScheduler &operator=(DiscoveryScheduler &&)=delete;
  DiscoveryTickResult tick();
  void stop() noexcept;
  // Runtime-side transport/health retirement also invalidates the scheduler's
  // retained evidence. Reconnection must rediscover BOTH fresh handle leases.
  void invalidate() noexcept;
  DiscoverySnapshot snapshot() override { return snapshot_; }
  DiscoveryScheduleState state() const { return state_; }
  std::string reason() const { return reason_; }
  std::uint64_t next_due_us() const { return due_us_; }
private:
  bool validate(const DiscoveryTaskResult &, std::uint64_t now, const char *&reason) const;
  bool read_clock(std::uint64_t &now) noexcept;
  void retire(const char *, DiscoveryTickResult * = nullptr) noexcept;
  void fail(const char *, std::uint64_t now, bool terminal, DiscoveryTickResult * = nullptr) noexcept;
  bool callback_time(std::uint64_t before, std::uint64_t &after, DiscoveryTickResult &);
  const SessionProfile profile_;
  const DiscoveryScheduleConfig config_;
  Clock &clock_;
  DiscoveryTaskProvider &provider_;
  DiscoveryCommitSink &sink_;
  DiscoveryScheduleState state_ = DiscoveryScheduleState::Denied;
  std::string reason_ = "UNBOUND_OR_SCHEDULING_DENIED";
  DiscoverySnapshot snapshot_;
  std::array<std::uint64_t,2> slot_observed_us_{};
  DiscoveryTaskRequest request_;
  std::unique_ptr<DiscoveryTask> task_;
  std::set<std::string> consumed_;
  std::uint64_t last_clock_ = 0, next_generation_ = 0, last_revision_ = 0;
  std::uint64_t due_us_ = 0, backoff_us_ = 0;
  bool clock_seen_ = false, invalidated_ = false;
};
}  // namespace openpilot::pandad::compatibility
