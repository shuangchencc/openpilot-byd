#include "transport.h"

#include <algorithm>

namespace openpilot::pandad::compatibility {

const char *status_name(Status status) {
  switch (status) {
    case Status::Ok: return "ok";
    case Status::Short: return "short";
    case Status::Timeout: return "timeout";
    case Status::Cancelled: return "cancelled";
    case Status::Disconnected: return "disconnected";
    case Status::Stall: return "stall";
    case Status::Busy: return "busy";
    case Status::Invalid: return "invalid";
    case Status::IoError: return "io_error";
    case Status::Poisoned: return "poisoned";
  }
  return "unknown";
}

bool is_read(Kind kind) {
  return kind == Kind::ControlRead || kind == Kind::BulkRead;
}

bool valid_request(const Request &request) {
  const bool control = request.kind == Kind::ControlRead || request.kind == Kind::ControlWrite;
  const bool bulk = request.kind == Kind::BulkRead || request.kind == Kind::BulkWrite;
  if (!control && !bulk) return false;
  if (request.endpoint > 0xffU || request.request > 0xffU ||
      request.value > 0xffffU || request.index > 0xffffU) return false;
  if (control) {
    if (request.endpoint != 0) return false;
  } else {
    const auto endpoint_number = request.endpoint & 0x7fU;
    if (endpoint_number == 0 || endpoint_number > 15 ||
        ((request.endpoint & 0x80U) != 0) != is_read(request.kind) ||
        request.request != 0 || request.value != 0 || request.index != 0) return false;
  }
  const auto maximum = control ? kMaxControlBytes : kMaxBulkBytes;
  if (is_read(request.kind)) {
    return request.payload.empty() && request.read_length > 0 && request.read_length <= maximum;
  }
  return request.read_length == 0 && request.payload.size() <= maximum &&
         (control || !request.payload.empty());
}

namespace {

bool known_status(Status status) {
  switch (status) {
    case Status::Ok: case Status::Short: case Status::Timeout:
    case Status::Cancelled: case Status::Disconnected: case Status::Stall:
    case Status::Busy: case Status::Invalid: case Status::IoError: case Status::Poisoned:
      return true;
  }
  return false;
}

// A backend cannot turn a malformed byte count into a successful transfer.
bool valid_result(const Result &result, const Request &request) {
  const auto expected = is_read(request.kind) ? request.read_length : request.payload.size();
  if (!known_status(result.status)) return false;
  if (is_read(request.kind) && result.delivery_uncertain) return false;
  if (result.actual > expected) return false;
  if (is_read(request.kind)) {
    if (result.data.size() != result.actual) return false;
  } else if (!result.data.empty()) {
    return false;
  }
  if (!result.actual_known) return result.status != Status::Ok && result.status != Status::Short;
  return result.status != Status::Short || result.actual < expected;
}

}  // namespace

Result Transport::perform(const Request &request, std::uint32_t timeout_ms) {
  if (!valid_request(request) || timeout_ms == 0 || timeout_ms > kMaxTransportTimeoutMs) {
    return {Status::Invalid};
  }
  if (cancelled_.load()) return {Status::Cancelled};
  if (poisoned_.load()) return {Status::Poisoned};
  const auto started = clock_.now_us();
  auto previous_time = started;
  const std::uint64_t budget_us = static_cast<std::uint64_t>(timeout_ms) * 1000U;
  const std::uint64_t lock_budget_us = std::min(budget_us, static_cast<std::uint64_t>(kLockWaitMs) * 1000U);
  // Subtraction only follows monotonicity checks; wrap/backward clocks never
  // turn expired work into a fresh deadline. We never add to the clock value.
  const auto elapsed = [&](std::uint64_t &value) {
    const auto now = clock_.now_us();
    if (now < previous_time) return false;
    previous_time = now;
    value = now - started;
    return true;
  };
  const auto clock_failure = [&]() {
    poisoned_.store(true);
    return Result{Status::IoError};
  };

  std::unique_lock<std::mutex> lock(mutex_, std::defer_lock);
  // Even a broken clock/adapter cannot cause an unbounded number of attempts.
  // A non-advancing clock is detected after the first paced wait below.
  while (true) {
    if (cancelled_.load()) return {Status::Cancelled};
    if (poisoned_.load()) return {Status::Poisoned};
    std::uint64_t spent = 0;
    if (!elapsed(spent)) return clock_failure();
    if (spent >= lock_budget_us) return {Status::Busy};
    if (lock.try_lock()) break;
    const auto before_wait = previous_time;
    clock_.wait_us(static_cast<std::uint32_t>(std::min<std::uint64_t>(kWaitQuantumUs, lock_budget_us - spent)));
    if (!elapsed(spent) || (previous_time == before_wait && !cancelled_.load())) return clock_failure();
  }

  Result previous_result;
  const auto finish = [&](Status status) {
    auto result = previous_result;
    result.status = status;
    return result;
  };
  while (true) {
    if (cancelled_.load()) return finish(Status::Cancelled);
    if (poisoned_.load()) return finish(Status::Poisoned);
    std::uint64_t spent = 0;
    if (!elapsed(spent)) {
      poisoned_.store(true);
      return finish(Status::IoError);
    }
    // libusb and adapter budgets have millisecond resolution. Do not round up
    // a sub-millisecond remainder and thereby schedule beyond the deadline.
    if (spent >= budget_us || budget_us - spent < 1000U) return finish(Status::Timeout);
    const auto slice = static_cast<std::uint32_t>(std::min<std::uint64_t>(kBackendSliceMs, (budget_us - spent) / 1000U));
    Result result;
    try {
      result = backend_.transact(request, slice);
    } catch (...) {
      result = {Status::IoError, 0, {}, false, !is_read(request.kind)};
    }
    result.backend_status = result.status;
    result.backend_attempted = true;
    if (!valid_result(result, request)) {
      // Contradictory adapter output cannot be used as a trustworthy count.
      result.status = Status::IoError;
      result.actual = 0;
      result.data.clear();
      result.actual_known = false;
      result.delivery_uncertain = !is_read(request.kind);
      poisoned_.store(true);
    } else if (result.status == Status::Ok && result.actual < (is_read(request.kind) ? request.read_length : request.payload.size())) {
      result.status = Status::Short;
    }

    // Latch terminal adapter failures before a later deadline/cancel changes
    // the outward status. A timeout label must not revive a disconnected board
    // or replay a request whose consumed stream extent is unknown.
    if (result.status == Status::Cancelled) cancelled_.store(true);
    if (result.status == Status::Disconnected || result.status == Status::Stall ||
        result.status == Status::IoError || result.status == Status::Poisoned ||
        (is_read(request.kind) && !result.actual_known)) poisoned_.store(true);

    std::uint64_t completed = 0;
    const bool clock_ok = elapsed(completed);
    if (!clock_ok) {
      result.status = Status::IoError;
      poisoned_.store(true);
    } else if (cancelled_.load()) {
      result.status = Status::Cancelled;
    } else if (completed >= budget_us || completed - spent >= static_cast<std::uint64_t>(slice) * 1000U) {
      result.status = Status::Timeout;
    }
    if (result.status == Status::Cancelled) cancelled_.store(true);
    if (!is_read(request.kind)) {
      if (result.status == Status::Ok && result.delivery_uncertain) result.status = Status::IoError;
      if (result.status != Status::Ok || result.delivery_uncertain) {
        result.delivery_uncertain = true;
        poisoned_.store(true);
      }
      return result;
    }
    if (result.status == Status::Disconnected || result.status == Status::Stall ||
        result.status == Status::IoError || result.status == Status::Poisoned) poisoned_.store(true);
    if (result.status != Status::Timeout || result.backend_status != Status::Timeout ||
        !result.actual_known || result.actual != 0 ||
        !clock_ok || completed >= budget_us || cancelled_.load() || poisoned_.load()) return result;
    // A backend can return an immediate timeout. Pace retries so even a virtual
    // clock makes forward progress and the loop cannot spin indefinitely.
    const auto remaining = budget_us - completed;
    previous_result = result;
    const auto before_wait = previous_time;
    clock_.wait_us(static_cast<std::uint32_t>(std::min<std::uint64_t>(kWaitQuantumUs, remaining)));
    if (!elapsed(completed) || (previous_time == before_wait && !cancelled_.load())) {
      poisoned_.store(true);
      return finish(Status::IoError);
    }
  }
}

}  // namespace openpilot::pandad::compatibility
