#include "../native_discovery_executor.h"

#include <chrono>
#include <condition_variable>
#include <functional>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <sys/wait.h>
#include <unistd.h>

using namespace openpilot::pandad::compatibility;
namespace {
unsigned assertions = 0;
#define CHECK(x) do { ++assertions; if (!(x)) throw std::runtime_error(std::string(__func__) + ":" + std::to_string(__LINE__) + ": " #x); } while (false)
using Steady = std::chrono::steady_clock;
struct FakeClock final : Clock {
  std::atomic<std::uint64_t> time{1000000};
  std::uint64_t now_us() override { return time.load(); }
  void wait_us(std::uint32_t n) override { time.fetch_add(n); }
};
SessionProfile profile() {
  SessionProfile p; p.schema = kNativeSessionProfileSchema; p.purpose = "native_read_only_candidate";
  p.binding_status = "NATIVE_READ_ONLY_BOUND";
  for (unsigned s = 0; s < 2; ++s) {
    p.slots[s].expected = {"uid:" + std::string(24, 'a' + s), s ? "RED_PANDA" : "DOS", s ? "H7" : "F4",
      s ? "STM32H725IGK6" : "STM32F413VGT6", s ? "SPI" : "USB", kSessionProtocol,
      {{0,1,2}, s == 1, s == 1, s == 1, true, s == 0, s == 0, false}, std::string(256, 'a' + s)};
    p.slots[s].safety_config_semantics = "noOutput";
  }
  p.roles = {1,1,1,{0,1},0,0,kRoleUnsupported}; return p;
}
class FakePort final : public NativeDiscoveryPort {
public:
  std::atomic<bool> enabled{true};
  std::atomic<unsigned> calls{0}, cancels{0}, proofs{0};
  std::atomic<unsigned> value{1};
  Result transact(const Request &r, std::uint32_t) override {
    ++calls;
    if (!enabled.load()) return {Status::Disconnected, 0, {}};
    return {Status::Ok, r.read_length, std::vector<std::uint8_t>(r.read_length, static_cast<std::uint8_t>(value.load()))};
  }
  void cancel() noexcept override { ++cancels; enabled = false; }
  bool active() const override { return enabled.load(); }
  std::optional<SpiFreshnessProof> live_spi_proof() override {
    ++proofs;
    if (!enabled.load()) return std::nullopt;
    SpiFreshnessProof p; p.synchronized = p.same_fd_lifetime_lock = true;
    p.first_fence_id = 100; p.second_fence_id = 101; p.completed_us = 999999; p.lease_nonce = value.load();
    p.expected_uid.fill(static_cast<std::uint8_t>(value.load())); return p;
  }
};
struct State {
  std::mutex mutex;
  std::condition_variable changed;
  bool block = false, block_retire = false, released = false, throws = false;
  unsigned entered = 0, retired = 0;
  bool destroyed = false, cancelled_seen = false, revoked = false, cleanup_before_revoke = false;
  std::thread::id execute_thread, retire_thread, destructor_thread;
  std::array<std::weak_ptr<FakePort>, 2> ports;
  void release() { std::lock_guard<std::mutex> lock(mutex); released = true; changed.notify_all(); }
  bool await_entered(unsigned count = 1) {
    std::unique_lock<std::mutex> lock(mutex);
    return changed.wait_for(lock, std::chrono::seconds(2), [&] { return entered >= count; });
  }
  std::shared_ptr<FakePort> port(std::size_t slot) { std::lock_guard<std::mutex> lock(mutex); return ports[slot].lock(); }
};
class FakeEngine final : public NativeDiscoveryEngine {
  std::shared_ptr<State> state_;
  std::shared_ptr<FakeClock> clock_;
  std::array<std::shared_ptr<FakePort>, 2> ports_;
public:
  FakeEngine(std::shared_ptr<State> state, std::shared_ptr<FakeClock> clock) : state_(std::move(state)), clock_(std::move(clock)) {}
  ~FakeEngine() override {
    std::lock_guard<std::mutex> lock(state_->mutex);
    state_->destroyed = true; state_->destructor_thread = std::this_thread::get_id(); state_->changed.notify_all();
  }
  DiscoveryTaskResult execute(const DiscoveryTaskRequest &request, const std::atomic<bool> &cancelled) override {
    {
      std::unique_lock<std::mutex> lock(state_->mutex);
      ++state_->entered; state_->execute_thread = std::this_thread::get_id(); state_->changed.notify_all();
      state_->changed.wait(lock, [&] { return !state_->block || state_->released; });
      state_->cancelled_seen |= cancelled.load();
      if (state_->throws) throw std::runtime_error("injected engine failure");
    }
    auto handoff = std::make_unique<NativeDiscoveryHandoff>(); handoff->profile = profile();
    DiscoveryTaskResult out; out.generation = request.generation; out.kind = request.kind;
    out.snapshot.revision = request.generation; out.snapshot.observed_us = clock_->now_us();
    out.slot_observed_us = {out.snapshot.observed_us, out.snapshot.observed_us};
    for (unsigned s = 0; s < 2; ++s) {
      if (request.kind == DiscoveryTaskKind::Discover || !ports_[s]) ports_[s] = std::make_shared<FakePort>();
      handoff->ports[s] = ports_[s];
      out.snapshot.items.push_back({handoff->profile.slots[s].expected,
        request.kind == DiscoveryTaskKind::Reprobe ? request.previous.items[s].handle_nonce :
          "lease-" + std::to_string(request.generation) + "-" + std::to_string(s), s == 0, s ? 100U : 0U});
    }
    { std::lock_guard<std::mutex> lock(state_->mutex); for (unsigned s = 0; s < 2; ++s) state_->ports[s] = ports_[s]; }
    out.payload = std::move(handoff); return out;
  }
  void retire() noexcept override {
    { std::unique_lock<std::mutex> lock(state_->mutex); state_->changed.wait(lock, [&] { return !state_->block_retire || state_->released; }); }
    for (auto &port : ports_) { if (port) port->cancel(); port.reset(); }
    std::lock_guard<std::mutex> lock(state_->mutex);
    ++state_->retired; state_->retire_thread = std::this_thread::get_id();
    state_->cleanup_before_revoke |= !state_->revoked; state_->changed.notify_all();
  }
};
DiscoveryTaskRequest request(std::uint64_t generation = 1) {
  DiscoveryTaskRequest r; r.generation = generation; r.started_us = 1000000; r.deadline_us = 1100000; return r;
}
bool await_lanes(std::size_t count) {
  const auto deadline = Steady::now() + std::chrono::seconds(2);
  while (Steady::now() < deadline) {
    if (NativeDiscoveryExecutor::occupied_worker_lanes() == count) return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  return false;
}
DiscoveryPollResult completed(DiscoveryTask &task) {
  const auto deadline = Steady::now() + std::chrono::seconds(2);
  while (Steady::now() < deadline) {
    auto p = task.poll();
    if (p.state != DiscoveryPollState::Pending) return p;
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  throw std::runtime_error("offline worker did not complete");
}
Request uid_read() { Request r; r.kind = Kind::ControlRead; r.request = 0xc3; r.read_length = 12; return r; }
struct Rig {
  std::shared_ptr<FakeClock> clock = std::make_shared<FakeClock>();
  std::shared_ptr<State> state = std::make_shared<State>();
  std::unique_ptr<NativeDiscoveryExecutor> executor;
  std::unique_ptr<DiscoveryTask> job;
  DiscoveryTaskRequest req = request();
  DiscoverySnapshot snapshot;
  explicit Rig(bool allow = true) {
    executor = std::make_unique<NativeDiscoveryExecutor>(std::make_unique<FakeEngine>(state, clock), clock,
      [state = state](const char *) { std::lock_guard<std::mutex> lock(state->mutex); state->revoked = true; }, allow);
  }
  ~Rig() {
    job.reset(); executor.reset(); state->release();
    bool ran = false; { std::lock_guard<std::mutex> lock(state->mutex); ran = state->entered != 0; }
    if (ran) (void)await_lanes(0);
  }
  void start() {
    unsigned entered; { std::lock_guard<std::mutex> lock(state->mutex); entered = state->entered; }
    job = executor->start(req); CHECK(job != nullptr); CHECK(state->await_entered(entered + 1));
  }
  void ready() {
    start(); auto p = completed(*job); CHECK(p.state == DiscoveryPollState::Complete); snapshot = p.result.snapshot;
    CHECK(executor->commit(req, p.result)); job.reset();
  }
  std::unique_ptr<OwnedTransport> open(unsigned slot = 0) { return executor->open(snapshot.items[slot], profile().slots[slot]); }
};
}  // namespace

int main() {
  unsigned passed = 0, failed = 0;
  const auto test = [&](const char *id, const std::function<void()> &fn) {
    try { CHECK(await_lanes(0)); fn(); CHECK(await_lanes(0)); ++passed; std::cout << "PASS " << id << '\n'; }
    catch (const std::exception &e) { ++failed; std::cerr << id << ": " << e.what() << '\n'; }
  };
  test("p10_executor_default_denied_has_no_worker", [] {
    Rig r(false); CHECK(!r.executor->start(r.req)); CHECK(NativeDiscoveryExecutor::occupied_worker_lanes() == 0);
    CHECK(r.state->entered == 0); CHECK(!r.executor->live_spi_proof(1));
  });
  test("p10_executor_constructor_inert_until_start", [] {
    Rig r; CHECK(NativeDiscoveryExecutor::occupied_worker_lanes() == 0); CHECK(r.state->entered == 0);
  });
  test("p10_executor_native_execution_on_worker", [] {
    Rig r; r.ready(); CHECK(NativeDiscoveryExecutor::occupied_worker_lanes() == 1);
    std::lock_guard<std::mutex> lock(r.state->mutex); CHECK(r.state->execute_thread != std::this_thread::get_id());
  });
  test("p10_executor_blocked_kernel_call_keeps_owner_nonblocking", [] {
    Rig r; r.state->block = true; r.start();
    auto begin = Steady::now();
    for (unsigned i = 0; i < 100; ++i) CHECK(r.job->poll().state == DiscoveryPollState::Pending);
    CHECK(Steady::now() - begin < std::chrono::milliseconds(500));
    CHECK(!r.executor->start(request(2))); CHECK(r.state->entered == 1);
  });
  test("p10_executor_cancelled_stuck_lane_prevents_new_worker", [] {
    Rig r; r.state->block = true; r.start();
    r.job->cancel(); r.job.reset(); CHECK(!r.executor->start(request(2)));
    Rig another; CHECK(!another.executor->start(request(1)));
    CHECK(NativeDiscoveryExecutor::occupied_worker_lanes() == 1); CHECK(another.state->entered == 0);
  });
  test("p10_executor_destructor_does_not_join_stuck_kernel", [] {
    Rig r; r.state->block = true; r.start(); r.job.reset();
    auto begin = Steady::now(); r.executor.reset();
    CHECK(Steady::now() - begin < std::chrono::milliseconds(500));
    CHECK(NativeDiscoveryExecutor::occupied_worker_lanes() == 1);
    { std::lock_guard<std::mutex> lock(r.state->mutex); CHECK(!r.state->destroyed); }
    r.state->release(); CHECK(await_lanes(0));
    std::lock_guard<std::mutex> lock(r.state->mutex);
    CHECK(r.state->destroyed); CHECK(r.state->destructor_thread != std::this_thread::get_id());
    CHECK(r.state->cancelled_seen);
  });
  test("p10_executor_lane_released_only_after_old_worker_exits", [] {
    Rig r; r.state->block = true; r.start(); r.job.reset(); r.executor.reset();
    Rig next; CHECK(!next.executor->start(next.req));
    r.state->release(); CHECK(await_lanes(0));
    next.ready(); CHECK(next.state->entered == 1);
  });
  test("p10_executor_task_destruction_cancels_late_result", [] {
    Rig r; r.state->block = true; r.start(); r.job.reset(); r.state->release();
    const auto deadline = Steady::now() + std::chrono::seconds(2);
    while (Steady::now() < deadline) {
      { std::lock_guard<std::mutex> lock(r.state->mutex); if (r.state->cancelled_seen) break; }
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    { std::lock_guard<std::mutex> lock(r.state->mutex); CHECK(r.state->cancelled_seen); }
    CHECK(!r.executor->live_spi_proof(1));
    auto p = profile(); CHECK(!r.executor->open({p.slots[0].expected, "lease-1-0", true, 0}, p.slots[0]));
  });
  test("p10_executor_throwing_engine_yields_failed_task", [] {
    Rig r; r.state->throws = true; r.start(); auto p = completed(*r.job);
    CHECK(p.state == DiscoveryPollState::Failed); CHECK(!r.executor->live_spi_proof(1));
  });
  test("p10_executor_complete_result_consumed_once", [] {
    Rig r; r.start(); auto p = completed(*r.job); CHECK(p.state == DiscoveryPollState::Complete);
    auto again = r.job->poll(); CHECK(!again.result.payload); CHECK(again.result.snapshot.items.empty());
    CHECK(r.executor->commit(r.req, p.result)); CHECK(!r.executor->commit(r.req, p.result));
  });
  test("p10_executor_fixed_slot_single_handoff", [] {
    Rig r; r.ready(); auto a = r.open(0); auto b = r.open(1);
    CHECK(a && b); CHECK(!r.open(0)); CHECK(!r.open(1));
    CHECK(a->transport->perform(uid_read(), 5).status == Status::Ok);
    CHECK(b->transport->perform(uid_read(), 5).status == Status::Ok);
    CHECK(r.state->port(0)->calls == 1 && r.state->port(1)->calls == 1);
  });
  test("p10_executor_handoff_exact_item_binding_and_safety", [] {
    Rig r; r.ready(); auto item = r.snapshot.items[0]; auto binding = profile().slots[0];
    item.handle_nonce = "other"; CHECK(!r.executor->open(item, binding)); item = r.snapshot.items[0];
    binding.safety_config_semantics = "allOutput"; CHECK(!r.executor->open(item, binding)); binding = profile().slots[0];
    item.usb_alt0_verified = false; CHECK(!r.executor->open(item, binding));
    CHECK(r.open(0));
  });
  test("p10_executor_proxy_readonly_even_if_port_is_permissive", [] {
    Rig r; r.ready(); auto owner = r.open(0); CHECK(owner); auto port = r.state->port(0); CHECK(port);
    auto read = uid_read(); Request write; write.kind = Kind::ControlWrite; write.request = 0xdc; write.value = 19;
    CHECK(owner->backend->transact(write, 1).status == Status::Invalid);
    write.kind = Kind::BulkWrite; write.endpoint = 3; write.payload = {1};
    CHECK(owner->backend->transact(write, 1).status == Status::Invalid);
    read.request = 0xd8; read.read_length = 1; CHECK(owner->backend->transact(read, 1).status == Status::Invalid);
    CHECK(port->calls == 0); CHECK(owner->backend->transact(uid_read(), 1).status == Status::Ok); CHECK(port->calls == 1);
  });
  test("p10_executor_proxy_validates_request_budget", [] {
    Rig r; r.ready(); auto owner = r.open(0); CHECK(owner); auto port = r.state->port(0);
    CHECK(owner->backend->transact(uid_read(), 0).status == Status::Invalid);
    CHECK(owner->backend->transact(uid_read(), kBackendSliceMs + 1).status == Status::Invalid);
    auto malformed = uid_read(); malformed.payload = {1};
    CHECK(owner->backend->transact(malformed, 1).status == Status::Invalid); CHECK(port->calls == 0);
  });
  test("p10_executor_retire_revokes_epochs_before_cleanup", [] {
    Rig r; r.ready(); auto a = r.open(0); auto b = r.open(1); CHECK(a && b);
    r.executor->retire("offline-test-retire");
    CHECK(a->transport->perform(uid_read(), 5).status != Status::Ok);
    CHECK(b->transport->perform(uid_read(), 5).status != Status::Ok);
    CHECK(!r.executor->live_spi_proof(1));
    r.job.reset(); r.executor.reset(); CHECK(await_lanes(0));
    std::lock_guard<std::mutex> lock(r.state->mutex);
    CHECK(r.state->revoked); CHECK(r.state->retired > 0); CHECK(!r.state->cleanup_before_revoke);
    CHECK(r.state->retire_thread != std::this_thread::get_id());
  });
  test("p10_executor_proxy_outlives_executor_without_native_ownership", [] {
    Rig r; r.ready(); auto owner = r.open(0); CHECK(owner);
    r.executor.reset(); CHECK(await_lanes(0));
    CHECK(owner->transport->perform(uid_read(), 5).status != Status::Ok); owner.reset();
    std::lock_guard<std::mutex> lock(r.state->mutex);
    CHECK(r.state->destroyed); CHECK(r.state->destructor_thread != std::this_thread::get_id());
  });
  test("p10_executor_live_spi_proof_not_cached", [] {
    Rig r; r.ready(); auto port = r.state->port(1); CHECK(port);
    auto first = r.executor->live_spi_proof(1); CHECK(first && first->lease_nonce == 1);
    port->value = 2; auto second = r.executor->live_spi_proof(1); CHECK(second && second->lease_nonce == 2);
    port->enabled = false; CHECK(!r.executor->live_spi_proof(1)); CHECK(!r.executor->live_spi_proof(2));
  });
  test("p10_executor_reprobe_retains_existing_proxy", [] {
    Rig r; r.ready(); auto owner = r.open(0); CHECK(owner); auto port = r.state->port(0);
    r.clock->time += 50000; r.req = request(2); r.req.kind = DiscoveryTaskKind::Reprobe; r.req.previous = r.snapshot;
    r.req.started_us = r.clock->now_us(); r.req.deadline_us = r.req.started_us + 100000;
    r.start(); auto p = completed(*r.job); CHECK(p.state == DiscoveryPollState::Complete);
    auto refreshed = p.result.snapshot;
    CHECK(r.executor->commit(r.req, p.result)); r.job.reset(); CHECK(r.state->port(0) == port);
    CHECK(!r.executor->commit(r.req, p.result));
    CHECK(!r.executor->open(refreshed.items[0], profile().slots[0]));
    CHECK(owner->transport->perform(uid_read(), 5).status == Status::Ok);
  });
  test("p10_executor_consumed_old_task_cancel_cannot_retire_new_task", [] {
    Rig r; r.start(); auto first = completed(*r.job); CHECK(first.state == DiscoveryPollState::Complete);
    r.snapshot = first.result.snapshot; CHECK(r.executor->commit(r.req, first.result));
    auto old_job = std::move(r.job); auto owner = r.open(0); CHECK(owner);
    r.clock->time += 50000; r.req = request(2); r.req.kind = DiscoveryTaskKind::Reprobe; r.req.previous = r.snapshot;
    r.req.started_us = r.clock->now_us(); r.req.deadline_us = r.req.started_us + 100000;
    { std::lock_guard<std::mutex> lock(r.state->mutex); r.state->block = true; }
    r.start(); old_job->cancel(); old_job.reset();
    CHECK(owner->transport->perform(uid_read(), 5).status == Status::Ok);
    r.state->release(); auto second = completed(*r.job); CHECK(second.state == DiscoveryPollState::Complete);
    CHECK(r.executor->commit(r.req, second.result)); CHECK(owner->transport->perform(uid_read(), 5).status == Status::Ok);
  });
  test("p10_executor_cancelled_reprobe_waits_for_owner_revoke_before_cleanup", [] {
    Rig r; r.ready(); auto owner = r.open(0); CHECK(owner);
    r.clock->time += 50000; r.req = request(2); r.req.kind = DiscoveryTaskKind::Reprobe; r.req.previous = r.snapshot;
    r.req.started_us = r.clock->now_us(); r.req.deadline_us = r.req.started_us + 100000;
    { std::lock_guard<std::mutex> lock(r.state->mutex); r.state->block = true; }
    r.start(); r.job->cancel(); r.job.reset(); r.state->release();
    const auto deadline = Steady::now() + std::chrono::seconds(2);
    while (Steady::now() < deadline) {
      { std::lock_guard<std::mutex> lock(r.state->mutex); if (r.state->cancelled_seen) break; }
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    { std::lock_guard<std::mutex> lock(r.state->mutex); CHECK(r.state->cancelled_seen); CHECK(r.state->retired == 0); }
    r.executor->retire("owner-revoked-cancelled-reprobe"); CHECK(owner->transport->perform(uid_read(), 5).status != Status::Ok);
    r.executor.reset(); CHECK(await_lanes(0));
    std::lock_guard<std::mutex> lock(r.state->mutex); CHECK(r.state->revoked); CHECK(!r.state->cleanup_before_revoke);
  });
  test("p10_executor_scheduler_end_to_end_reprobe_timeout_reconnect", [] {
    // This connects the real scheduler and executor interfaces with an injected
    // engine. Native providers remain receive-only; this is not a grant to send
    // heartbeat/safety writes or a passing production P4 health barrier.
    Rig r;
    DiscoveryScheduleConfig config; config.allow_scheduling = true;
    DiscoveryScheduler scheduler(profile(), *r.clock, *r.executor, *r.executor, config);
    const auto await_ready = [&] {
      const auto deadline = Steady::now() + std::chrono::seconds(2);
      while (Steady::now() < deadline) {
        auto tick = scheduler.tick();
        if (tick.committed) return;
        if (tick.state == DiscoveryScheduleState::Failed || tick.state == DiscoveryScheduleState::Denied)
          throw std::runtime_error("scheduler unexpectedly denied: " + tick.reason);
        if (tick.state == DiscoveryScheduleState::Backoff) r.clock->time = scheduler.next_due_us();
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
      }
      throw std::runtime_error("scheduler/executor did not commit");
    };
    CHECK(scheduler.tick().started); CHECK(r.state->await_entered(1)); await_ready();
    auto initial = scheduler.snapshot(); CHECK(initial.items.size() == 2);
    auto owner = r.executor->open(initial.items[0], profile().slots[0]); CHECK(owner);
    auto first_port = r.state->port(0); CHECK(first_port);
    r.clock->time = scheduler.next_due_us(); CHECK(scheduler.tick().started);
    CHECK(r.state->await_entered(2)); await_ready(); auto refreshed = scheduler.snapshot();
    CHECK(refreshed.revision > initial.revision); CHECK(refreshed.observed_us > initial.observed_us);
    CHECK(refreshed.items[0].handle_nonce == initial.items[0].handle_nonce); CHECK(r.state->port(0) == first_port);
    CHECK(owner->transport->perform(uid_read(), 5).status == Status::Ok);
    { std::lock_guard<std::mutex> lock(r.state->mutex); r.state->block = true; r.state->released = false; }
    r.clock->time = scheduler.next_due_us(); CHECK(scheduler.tick().started); CHECK(r.state->await_entered(3));
    r.clock->time += config.task_timeout_us; auto timeout = scheduler.tick();
    CHECK(timeout.reason == "DISCOVERY_TASK_TIMEOUT"); CHECK(timeout.retired); CHECK(scheduler.snapshot().items.empty());
    { std::lock_guard<std::mutex> lock(r.state->mutex); CHECK(r.state->revoked); }
    CHECK(owner->transport->perform(uid_read(), 5).status != Status::Ok);
    r.state->release(); r.clock->time = scheduler.next_due_us(); await_ready();
    auto reconnected = scheduler.snapshot(); CHECK(reconnected.items.size() == 2);
    CHECK(reconnected.items[0].handle_nonce != initial.items[0].handle_nonce);
    CHECK(reconnected.items[1].handle_nonce != initial.items[1].handle_nonce);
    auto replacement = r.executor->open(reconnected.items[0], profile().slots[0]); CHECK(replacement);
    CHECK(replacement->transport->perform(uid_read(), 5).status == Status::Ok);
    CHECK(owner->transport->perform(uid_read(), 5).status != Status::Ok);
  });
  test("p10_executor_polled_result_cannot_commit_after_retire", [] {
    Rig r; r.state->block_retire = true; r.start(); auto result = completed(*r.job);
    CHECK(result.state == DiscoveryPollState::Complete);
    r.executor->retire("before-handoff");
    CHECK(!r.executor->commit(r.req, result.result)); CHECK(!r.executor->live_spi_proof(1));
  });
  test("p10_executor_fabricated_result_without_job_rejected", [] {
    Rig r; DiscoveryTaskResult result; result.generation = r.req.generation; result.kind = r.req.kind;
    result.snapshot.revision = 1; result.snapshot.observed_us = r.clock->now_us(); result.slot_observed_us = {r.clock->now_us(), r.clock->now_us()};
    auto payload = std::make_unique<NativeDiscoveryHandoff>(); payload->profile = profile();
    for (unsigned s = 0; s < 2; ++s) {
      payload->ports[s] = std::make_shared<FakePort>();
      result.snapshot.items.push_back({payload->profile.slots[s].expected, "fabricated-" + std::to_string(s), s == 0, s ? 100U : 0U});
    }
    result.payload = std::move(payload); CHECK(!r.executor->commit(r.req, result));
    CHECK(NativeDiscoveryExecutor::occupied_worker_lanes() == 0);
  });
  test("p10_executor_external_owner_retains_clock_until_destroyed", [] {
    Rig r; r.ready(); auto owner = r.open(0); CHECK(owner);
    std::weak_ptr<Clock> clock_lifetime = r.clock;
    r.executor.reset(); CHECK(await_lanes(0)); r.clock.reset();
    CHECK(!clock_lifetime.expired());
    CHECK(owner->transport->perform(uid_read(), 5).status != Status::Ok);
    owner.reset(); CHECK(clock_lifetime.expired());
  });
  test("p10_executor_fork_child_cannot_use_or_destroy_parent_session", [] {
    Rig r; r.ready(); auto owner = r.open(0); CHECK(owner);
    auto port = r.state->port(0); CHECK(port); auto item = r.snapshot.items[0]; auto binding = profile().slots[0];
    auto read = uid_read();
    r.clock->time += 50000; r.req = request(2); r.req.kind = DiscoveryTaskKind::Reprobe; r.req.previous = r.snapshot;
    r.req.started_us = r.clock->now_us(); r.req.deadline_us = r.req.started_us + 100000;
    { std::lock_guard<std::mutex> lock(r.state->mutex); r.state->block = true; }
    r.start();
    const auto calls = port->calls.load(), cancels = port->cancels.load(), proofs = port->proofs.load();
    // The worker is deliberately alive at fork. The child exercises only the
    // PID guards and requested object destruction, never ordinary process exit,
    // fixture assertions/stdio, discovery, test engine locks or native I/O.
    // alarm bounds an accidental inherited-mutex/destructor hang.
    const pid_t child = fork(); CHECK(child >= 0);
    if (child == 0) {
      alarm(2);
      if (r.executor->start(r.req)) _exit(10);
      if (r.job->poll().state != DiscoveryPollState::Failed) _exit(11);
      if (r.executor->open(item, binding)) _exit(12);
      if (r.executor->live_spi_proof(1)) _exit(13);
      if (owner->backend->transact(read, 1).status != Status::Cancelled) _exit(14);
      r.job->cancel(); r.executor->retire("fork-child-must-not-revoke");
      r.job.reset(); r.executor.reset(); owner.reset();
      if (port->calls.load() != calls || port->cancels.load() != cancels || port->proofs.load() != proofs) _exit(15);
      if (r.state->destroyed || r.state->retired || r.state->revoked) _exit(16);
      if (NativeDiscoveryExecutor::occupied_worker_lanes() != 1) _exit(17);
      _exit(0);
    }
    int status = 0; CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status)); CHECK(WEXITSTATUS(status) == 0);
    CHECK(NativeDiscoveryExecutor::occupied_worker_lanes() == 1);
    CHECK(port->calls == calls && port->cancels == cancels && port->proofs == proofs);
    CHECK(owner->transport->perform(read, 5).status == Status::Ok);
    r.state->release(); auto result = completed(*r.job); CHECK(result.state == DiscoveryPollState::Complete);
    CHECK(r.executor->commit(r.req, result.result)); CHECK(owner->transport->perform(read, 5).status == Status::Ok);
  });
  std::cout << "{\"suite\":\"p10_native_discovery_executor\",\"passed\":" << passed << ",\"failed\":" << failed
            << ",\"skipped\":0,\"assertions\":" << assertions << ",\"real_device_io\":false}\n";
  return failed ? 1 : 0;
}
