#include "../transport.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <functional>
#include <future>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>

using namespace openpilot::pandad::compatibility;
using Bytes = std::vector<std::uint8_t>;

namespace {

std::size_t assertions = 0;
void check(bool value, int line) {
  ++assertions;
  if (!value) throw std::runtime_error("assertion at line " + std::to_string(line));
}
#define CHECK(value) check(static_cast<bool>(value), __LINE__)

class FakeClock final : public Clock {
public:
  std::atomic<std::uint64_t> time{0};
  std::function<void()> on_now;
  std::function<void()> on_wait;
  bool advance_on_wait = true;
  std::uint64_t now_us() override {
    if (on_now) on_now();
    return time.load();
  }
  void wait_us(std::uint32_t duration) override {
    if (duration == 0 || duration > kWaitQuantumUs) throw std::runtime_error("invalid wait quantum");
    if (advance_on_wait) time.fetch_add(duration);
    if (on_wait) on_wait();
  }
};

struct Step {
  Result result;
  std::uint64_t duration = 0;
  std::function<void()> action{};
};

class FakeBackend final : public Backend {
public:
  explicit FakeBackend(FakeClock &clock) : clock(clock) {}
  FakeClock &clock;
  std::deque<Step> script;
  std::vector<Request> calls;
  std::vector<std::uint32_t> budgets;
  std::atomic<std::size_t> cancellations{0};
  Result transact(const Request &request, std::uint32_t timeout_ms) override {
    calls.push_back(request);
    budgets.push_back(timeout_ms);
    if (script.empty()) throw std::runtime_error("unscripted backend call");
    auto step = std::move(script.front()); script.pop_front();
    clock.time.fetch_add(step.duration);
    if (step.action) step.action();
    return step.result;
  }
  void cancel() noexcept override { cancellations.fetch_add(1); }
};

Request bulk_read(std::size_t size = 4) { return {Kind::BulkRead, 0x81, 0, 0, 0, size, {}}; }
Request bulk_write() { return {Kind::BulkWrite, 3, 0, 0, 0, 0, {1, 2, 3, 4}}; }
Result read_result(Bytes bytes, Status status = Status::Ok) {
  return {status, bytes.size(), std::move(bytes), true, false};
}

// The condition variable is a deterministic barrier, not a timing assertion.
// Its bounded real-time wait is only a fail-fast safeguard for a broken test.
class BlockingBackend final : public Backend {
public:
  std::mutex mutex;
  std::condition_variable condition;
  bool entered = false;
  bool release = false;
  std::size_t calls = 0;
  Result transact(const Request &, std::uint32_t) override {
    std::unique_lock<std::mutex> lock(mutex);
    ++calls; entered = true; condition.notify_all();
    if (!condition.wait_for(lock, std::chrono::seconds(2), [&] { return release; })) {
      throw std::runtime_error("test barrier release missing");
    }
    return read_result({1, 2, 3, 4});
  }
  bool wait_entered() {
    std::unique_lock<std::mutex> lock(mutex);
    return condition.wait_for(lock, std::chrono::seconds(2), [&] { return entered; });
  }
  void unblock() {
    std::lock_guard<std::mutex> lock(mutex);
    release = true; condition.notify_all();
  }
};

}  // namespace

int main() {
  std::size_t groups = 0;
  std::vector<std::string> names;
  const auto test = [&](const char *name, const std::function<void()> &run) {
    run(); ++groups; names.emplace_back(name); std::cout << "PASS " << name << '\n';
  };
  try {
    test("constructor_and_wide_ingress_validation_have_no_io", [] {
      FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
      CHECK(backend.calls.empty()); CHECK(transport.healthy());
      auto request = bulk_read();
      for (const auto endpoint : {0U, 1U, 0x80U, 0x90U, 0x181U, 0xffffffffU}) {
        request.endpoint = endpoint;
        CHECK(transport.perform(request).status == Status::Invalid);
      }
      request = bulk_write(); request.endpoint = 0x83;
      CHECK(transport.perform(request).status == Status::Invalid);
      request = bulk_read(); request.read_length = kMaxBulkBytes + 1;
      CHECK(transport.perform(request).status == Status::Invalid);
      request.read_length = 0; CHECK(!valid_request(request));
      request = bulk_read(); request.payload = {1}; CHECK(!valid_request(request));
      request = bulk_write(); request.read_length = 1; CHECK(!valid_request(request));
      request = bulk_write(); request.payload.clear(); CHECK(!valid_request(request));
      request = bulk_read(); request.request = 1; CHECK(!valid_request(request));
      request = {Kind::ControlRead, 0, 0xdd, 0xffff, 0xffff, 8, {}};
      CHECK(valid_request(request));
      request.request = 0x100; CHECK(!valid_request(request));
      request.request = 0xdd; request.value = 0x10000; CHECK(!valid_request(request));
      request.value = 0; request.index = 0x10000; CHECK(!valid_request(request));
      request.index = 0; request.endpoint = 1; CHECK(!valid_request(request));
      request.endpoint = 0; request.read_length = kMaxControlBytes + 1; CHECK(!valid_request(request));
      request.kind = static_cast<Kind>(999); CHECK(!valid_request(request));
      CHECK(transport.perform(bulk_read(), 0).status == Status::Invalid);
      CHECK(transport.perform(bulk_read(), 101).status == Status::Invalid);
      CHECK(backend.calls.empty()); CHECK(clock.time == 0);
    });
    test("exact_read_and_zero_payload_control_write", [] {
      FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
      backend.script.push_back({read_result({9, 8, 7, 6})});
      auto result = transport.perform(bulk_read());
      CHECK(result.status == Status::Ok); CHECK(result.actual == 4); CHECK(result.data == Bytes({9, 8, 7, 6}));
      CHECK(result.backend_attempted); CHECK(result.backend_status == Status::Ok);
      backend.script.push_back({{Status::Ok, 0, {}, true, false}});
      result = transport.perform({Kind::ControlWrite, 0, 0xf1, 0, 0, 0, {}});
      CHECK(result.status == Status::Ok); CHECK(result.actual == 0); CHECK(transport.healthy());
      CHECK(backend.calls.size() == 2); CHECK(backend.budgets == std::vector<std::uint32_t>({5, 5}));
    });
    test("short_read_and_fragment_boundaries_are_preserved", [] {
      FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
      backend.script.push_back({read_result({1, 2, 3})});
      backend.script.push_back({read_result({4, 5})});
      const auto first = transport.perform(bulk_read(8));
      CHECK(first.status == Status::Short); CHECK(first.actual == 3); CHECK(backend.calls.size() == 1);
      const auto second = transport.perform(bulk_read(8));
      CHECK(second.status == Status::Short); CHECK(second.actual == 2); CHECK(backend.calls.size() == 2);
      Bytes combined = first.data; combined.insert(combined.end(), second.data.begin(), second.data.end());
      CHECK(combined == Bytes({1, 2, 3, 4, 5})); CHECK(transport.healthy());
      backend.script.push_back({read_result({}, Status::Short)});
      CHECK(transport.perform(bulk_read()).status == Status::Short); CHECK(backend.calls.size() == 3);
    });
    test("read_retries_only_known_empty_timeout_and_is_paced", [] {
      FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
      backend.script.push_back({{Status::Timeout}});
      backend.script.push_back({{Status::Timeout}});
      backend.script.push_back({read_result({1, 2, 3, 4})});
      auto result = transport.perform(bulk_read(), 10);
      CHECK(result.status == Status::Ok); CHECK(backend.calls.size() == 3); CHECK(clock.time == 2000);
      CHECK(backend.budgets == std::vector<std::uint32_t>({5, 5, 5}));
      FakeClock immediate_clock; FakeBackend immediate(immediate_clock); Transport bounded(immediate, immediate_clock);
      for (int i = 0; i < 3; ++i) immediate.script.push_back({{Status::Timeout}});
      result = bounded.perform(bulk_read(), 3);
      CHECK(result.status == Status::Timeout); CHECK(result.backend_attempted); CHECK(result.backend_status == Status::Timeout);
      CHECK(immediate.calls.size() == 3); CHECK(immediate_clock.time == 3000);
      CHECK(immediate.budgets == std::vector<std::uint32_t>({3, 2, 1}));
    });
    test("partial_unknown_or_non_timeout_reads_never_retry", [] {
      for (const auto status : {Status::Timeout, Status::Cancelled, Status::Disconnected, Status::Stall, Status::Busy, Status::IoError}) {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        backend.script.push_back({read_result({1, 2}, status)});
        const auto result = transport.perform(bulk_read());
        CHECK(result.status == status); CHECK(result.actual == 2); CHECK(result.data == Bytes({1, 2})); CHECK(backend.calls.size() == 1);
        if (status == Status::Cancelled) {
          CHECK(!transport.healthy()); CHECK(transport.perform(bulk_read()).status == Status::Cancelled); CHECK(backend.calls.size() == 1);
        }
      }
      FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
      backend.script.push_back({{Status::Timeout, 0, {}, false, false}});
      const auto result = transport.perform(bulk_read());
      CHECK(result.status == Status::Timeout); CHECK(!result.actual_known); CHECK(backend.calls.size() == 1);
      CHECK(!transport.healthy()); CHECK(transport.perform(bulk_read()).status == Status::Poisoned); CHECK(backend.calls.size() == 1);
    });
    test("confirmed_prefix_survives_unknown_later_chunk_delivery", [] {
      for (const bool write : {false, true}) {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        backend.script.push_back({{Status::Timeout, 2, write ? Bytes{} : Bytes{1, 2}, false, write}});
        const auto result = transport.perform(write ? bulk_write() : bulk_read());
        CHECK(result.status == Status::Timeout); CHECK(result.actual == 2); CHECK(!result.actual_known);
        CHECK(result.data == (write ? Bytes{} : Bytes{1, 2})); CHECK(result.delivery_uncertain == write);
        CHECK(backend.calls.size() == 1); CHECK(!transport.healthy());
        CHECK(transport.perform(bulk_read()).status == Status::Poisoned); CHECK(backend.calls.size() == 1);
      }
    });
    test("late_fatal_backend_status_is_latched_before_deadline_override", [] {
      for (const auto status : {Status::Disconnected, Status::Stall, Status::IoError, Status::Poisoned, Status::Cancelled}) {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        backend.script.push_back({{status}, 5000});
        const auto result = transport.perform(bulk_read(), 100);
        CHECK(result.status == (status == Status::Cancelled ? Status::Cancelled : Status::Timeout));
        CHECK(result.backend_status == status); CHECK(result.backend_attempted); CHECK(result.actual == 0);
        CHECK(backend.calls.size() == 1); CHECK(!transport.healthy());
        CHECK(transport.perform(bulk_read()).status == (status == Status::Cancelled ? Status::Cancelled : Status::Poisoned));
        CHECK(backend.calls.size() == 1);
      }
      for (const auto status : {Status::Ok, Status::Short, Status::Busy, Status::Invalid}) {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        backend.script.push_back({{status}, 5000});
        const auto result = transport.perform(bulk_read(), 100);
        CHECK(result.status == Status::Timeout); CHECK(result.backend_status == status); CHECK(backend.calls.size() == 1);
      }
    });
    test("failed_and_uncertain_writes_are_never_replayed_and_poison_session", [] {
      for (const auto status : {Status::Short, Status::Timeout, Status::Cancelled, Status::Disconnected, Status::Stall, Status::Busy, Status::IoError}) {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        backend.script.push_back({{status, 2, {}, true, false}});
        const auto result = transport.perform(bulk_write());
        CHECK(result.status == status); CHECK(result.actual == 2); CHECK(result.actual_known); CHECK(result.delivery_uncertain);
        CHECK(result.backend_status == status); CHECK(backend.calls.size() == 1); CHECK(!transport.healthy());
        CHECK(transport.perform(bulk_read()).status == (status == Status::Cancelled ? Status::Cancelled : Status::Poisoned)); CHECK(backend.calls.size() == 1);
      }
      FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
      backend.script.push_back({{Status::Ok, 4, {}, true, true}});
      const auto result = transport.perform(bulk_write());
      CHECK(result.status == Status::IoError); CHECK(result.actual == 4); CHECK(result.backend_status == Status::Ok);
      CHECK(result.delivery_uncertain); CHECK(!transport.healthy()); CHECK(backend.calls.size() == 1);
    });
    test("deadline_minus_one_exact_and_plus_one_preserve_late_counts", [] {
      for (const bool write : {false, true}) for (const auto duration : {4999U, 5000U, 5001U}) {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        backend.script.push_back({write ? Result{Status::Ok, 4, {}, true, false} : read_result({1, 2, 3, 4}), duration});
        const auto result = transport.perform(write ? bulk_write() : bulk_read(), 5);
        CHECK(result.status == (duration < 5000 ? Status::Ok : Status::Timeout));
        CHECK(result.actual == 4); CHECK(result.data == (write ? Bytes{} : Bytes({1, 2, 3, 4}))); CHECK(result.backend_status == Status::Ok);
        CHECK(result.delivery_uncertain == (write && duration >= 5000));
        CHECK(backend.calls.size() == 1); CHECK(backend.budgets[0] == 5);
      }
      FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
      backend.script.push_back({{Status::Ok, 4, {}, true, false}, 5000});
      const auto result = transport.perform(bulk_write(), 100);
      CHECK(result.status == Status::Timeout); CHECK(result.actual == 4); CHECK(result.delivery_uncertain);
      CHECK(!transport.healthy()); CHECK(backend.calls.size() == 1);
    });
    test("submillisecond_remaining_budget_does_not_round_up", [] {
      FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
      backend.script.push_back({{Status::Timeout}, 4999});
      backend.script.push_back({{Status::Timeout}, 3002});
      const auto result = transport.perform(bulk_read(), 10);
      CHECK(result.status == Status::Timeout); CHECK(result.backend_attempted); CHECK(backend.calls.size() == 2);
      CHECK(backend.budgets == std::vector<std::uint32_t>({5, 4})); CHECK(clock.time == 10000);
    });
    test("cancellation_before_during_after_and_between_retries_is_permanent", [] {
      {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        transport.cancel(); CHECK(!transport.healthy()); CHECK(backend.cancellations == 1);
        const auto result = transport.perform(bulk_read());
        CHECK(result.status == Status::Cancelled); CHECK(!result.backend_attempted); CHECK(backend.calls.empty());
      }
      for (const bool write : {false, true}) for (const auto duration : {4999U, 5000U, 5001U}) {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        const Result success = write ? Result{Status::Ok, 4, {}, true, false} : read_result({1, 2, 3, 4});
        backend.script.push_back({success, duration, [&] { transport.cancel(); }});
        const auto result = transport.perform(write ? bulk_write() : bulk_read());
        CHECK(result.status == Status::Cancelled); CHECK(result.actual == 4); CHECK(result.backend_status == Status::Ok);
        CHECK(result.delivery_uncertain == write); CHECK(backend.calls.size() == 1); CHECK(!transport.healthy());
      }
      {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        std::size_t now_calls = 0;
        clock.on_now = [&] { if (++now_calls == 4) transport.cancel(); };
        backend.script.push_back({read_result({1, 2, 3, 4})});
        const auto result = transport.perform(bulk_read());
        CHECK(result.status == Status::Cancelled); CHECK(result.actual == 4); CHECK(result.backend_status == Status::Ok);
      }
      {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        backend.script.push_back({{Status::Timeout}});
        clock.on_wait = [&] { transport.cancel(); };
        const auto result = transport.perform(bulk_read());
        CHECK(result.status == Status::Cancelled); CHECK(result.backend_attempted); CHECK(result.backend_status == Status::Timeout);
        CHECK(backend.calls.size() == 1);
      }
    });
    test("backward_or_wrapped_clock_fails_closed_without_losing_actual", [] {
      for (const bool wrap : {false, true}) {
        FakeClock clock; clock.time = wrap ? std::numeric_limits<std::uint64_t>::max() - 5U : 100;
        FakeBackend backend(clock); Transport transport(backend, clock);
        backend.script.push_back({read_result({1, 2, 3, 4}), wrap ? 10U : 0U,
                                  wrap ? std::function<void()>{} : std::function<void()>{[&] { clock.time = 99; }}});
        const auto result = transport.perform(bulk_read());
        CHECK(result.status == Status::IoError); CHECK(result.actual == 4); CHECK(result.backend_status == Status::Ok);
        CHECK(!transport.healthy()); CHECK(backend.calls.size() == 1);
      }
      FakeClock clock; clock.time = std::numeric_limits<std::uint64_t>::max() - 2000U;
      FakeBackend backend(clock); Transport transport(backend, clock);
      backend.script.push_back({read_result({1, 2, 3, 4}), 999});
      CHECK(transport.perform(bulk_read(), 1).status == Status::Ok);
    });
    test("nonadvancing_clock_does_not_create_unbounded_retry_loop", [] {
      FakeClock clock; clock.advance_on_wait = false; FakeBackend backend(clock); Transport transport(backend, clock);
      backend.script.push_back({{Status::Timeout}});
      const auto result = transport.perform(bulk_read());
      CHECK(result.status == Status::IoError); CHECK(!transport.healthy()); CHECK(result.backend_attempted);
      CHECK(result.backend_status == Status::Timeout); CHECK(backend.calls.size() == 1);
    });
    test("malformed_backend_results_never_become_success", [] {
      const std::vector<Result> malformed = {
        {Status::Ok, 5, {1, 2, 3, 4, 5}, true, false},
        {Status::Ok, 4, {1, 2}, true, false},
        {Status::Ok, 0, {}, false, false},
        {Status::Timeout, 2, {1}, false, false},
        {Status::Short, 4, {1, 2, 3, 4}, true, false},
        {static_cast<Status>(999), 0, {}, true, false},
        {Status::Timeout, 0, {}, false, true},
      };
      for (const auto &bad : malformed) {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        backend.script.push_back({bad});
        const auto result = transport.perform(bulk_read());
        CHECK(result.status == Status::IoError); CHECK(!result.actual_known); CHECK(result.actual == 0); CHECK(result.data.empty());
        CHECK(!transport.healthy()); CHECK(backend.calls.size() == 1);
      }
    });
    test("backend_exceptions_preserve_unknown_delivery_and_never_retry", [] {
      for (const bool write : {false, true}) {
        FakeClock clock; FakeBackend backend(clock); Transport transport(backend, clock);
        const auto result = transport.perform(write ? bulk_write() : bulk_read());
        CHECK(result.status == Status::IoError); CHECK(!result.actual_known); CHECK(result.delivery_uncertain == write);
        CHECK(result.backend_attempted); CHECK(!transport.healthy()); CHECK(backend.calls.size() == 1);
      }
    });
    test("blocked_board_does_not_block_other_board_or_mix_bytes", [] {
      FakeClock clock_a, clock_b; BlockingBackend backend_a; FakeBackend backend_b(clock_b);
      Transport board_a(backend_a, clock_a), board_b(backend_b, clock_b);
      auto pending = std::async(std::launch::async, [&] { return board_a.perform(bulk_read()); });
      const bool entered = backend_a.wait_entered();
      backend_b.script.push_back({read_result({9, 8, 7, 6})});
      const auto independent = board_b.perform(bulk_read());
      board_a.cancel();
      backend_a.unblock();
      const auto cancelled = pending.get();
      CHECK(entered); CHECK(independent.status == Status::Ok); CHECK(independent.data == Bytes({9, 8, 7, 6}));
      CHECK(cancelled.status == Status::Cancelled); CHECK(cancelled.data == Bytes({1, 2, 3, 4}));
      CHECK(board_b.healthy()); CHECK(!board_a.healthy()); CHECK(backend_a.calls == 1); CHECK(backend_b.calls.size() == 1);
    });
    test("same_board_contender_wait_is_bounded_without_queueing_io", [] {
      for (const auto budget : {1U, 10U, 100U}) {
        FakeClock clock; BlockingBackend backend; Transport transport(backend, clock);
        auto pending = std::async(std::launch::async, [&] { return transport.perform(bulk_read(), 100); });
        const bool entered = backend.wait_entered();
        const auto contender = transport.perform(bulk_read(), budget);
        transport.cancel(); backend.unblock(); const auto first = pending.get();
        CHECK(entered); CHECK(contender.status == Status::Busy); CHECK(!contender.backend_attempted);
        CHECK(clock.time == static_cast<std::uint64_t>(std::min(budget, 10U)) * 1000U);
        CHECK(first.status == Status::Cancelled); CHECK(backend.calls == 1);
      }
    });
    test("lock_release_deadline_minus_one_exact_plus_one", [] {
      for (const auto release_us : {9999U, 10000U, 10001U}) {
        FakeClock clock; BlockingBackend backend; Transport transport(backend, clock);
        auto pending = std::async(std::launch::async, [&] { return transport.perform(bulk_read(), 100); });
        const bool entered = backend.wait_entered();
        bool released = false;
        clock.on_wait = [&] {
          if (!released && clock.time >= 9000) {
            released = true; clock.time = release_us; backend.unblock(); pending.wait();
          }
        };
        const auto contender = transport.perform(bulk_read(), 100);
        backend.unblock(); (void)pending.get();
        CHECK(entered); CHECK(released);
        CHECK(contender.status == (release_us < 10000 ? Status::Ok : Status::Busy));
        CHECK(backend.calls == (release_us < 10000 ? 2U : 1U));
      }
    });
    test("disconnect_stall_and_poison_are_session_local", [] {
      FakeClock first_clock, second_clock; FakeBackend first(first_clock), second(second_clock);
      Transport first_transport(first, first_clock), second_transport(second, second_clock);
      first.script.push_back({{Status::Disconnected}}); second.script.push_back({read_result({5, 6, 7, 8})});
      CHECK(first_transport.perform(bulk_read()).status == Status::Disconnected);
      CHECK(first_transport.perform(bulk_read()).status == Status::Poisoned); CHECK(first.calls.size() == 1);
      const auto result = second_transport.perform(bulk_read());
      CHECK(result.status == Status::Ok); CHECK(result.data == Bytes({5, 6, 7, 8})); CHECK(second_transport.healthy());
    });
    std::cout << "{\"passed\":" << groups << ",\"failed\":0,\"assertions\":" << assertions
              << ",\"device_access\":false,\"clock\":\"deterministic_fake\",\"hardware_timing_validated\":false,\"cases\":[";
    for (std::size_t i = 0; i < names.size(); ++i) std::cout << (i == 0 ? "\"" : ",\"") << names[i] << '"';
    std::cout << "]}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "FAIL after " << groups << " groups: " << error.what() << '\n';
    return 1;
  }
}
