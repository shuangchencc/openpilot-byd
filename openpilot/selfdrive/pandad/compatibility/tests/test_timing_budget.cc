#include "timing_budget.h"
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
using namespace openpilot::pandad::compatibility;
int main() {
  unsigned checks = 0;
  auto check = [&](bool ok) { ++checks; if (!ok) throw std::runtime_error("timing policy failure"); };
  for (std::uint64_t start : {std::uint64_t{0}, std::numeric_limits<std::uint64_t>::max() - 3}) {
    for (auto age : {kHealthMaxAgeUs - 1, kHealthMaxAgeUs, kHealthMaxAgeUs + 1}) {
      check(health_fresh(true, start + age, start) == (age < kHealthMaxAgeUs));
      check(!health_fresh(false, start + age, start));
      check(heartbeat_overdue(true, start + age, start) == (age >= kHeartbeatMaxAgeUs));
    }
    for (auto age : {kHeartbeatPeriodUs - 1, kHeartbeatPeriodUs, kHeartbeatPeriodUs + 1}) {
      check(heartbeat_due(true, start + age, start) == (age >= kHeartbeatPeriodUs));
    }
    check(!health_fresh(true, start, start + 1));
    check(heartbeat_due(true, start, start + 1));
    check(heartbeat_overdue(true, start, start + 1));
  }
  check(heartbeat_due(false, 0, 0));
  check(heartbeat_overdue(false, 0, 0));
  check(!valid_elapsed(std::uint64_t{1} << 63, 0));
  for (std::size_t inflight = 0; inflight <= 2; ++inflight) {
    for (std::size_t pending = 0; pending <= 1; ++pending) {
      check(queue_admissible(inflight, pending) == (inflight == 0 && pending == 0));
    }
  }
  std::cout << "{\"suite\":\"timing_policy\",\"passed\":4,\"failed\":0,\"assertions\":" << checks
            << ",\"cases\":[\"health_age_boundaries_wrap\",\"heartbeat_boundaries_wrap\",\"future_missing_rejected\",\"queue_capacity_boundaries\"]}\n";
}
