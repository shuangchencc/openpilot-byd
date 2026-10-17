#include "../spi_backend.h"

#include <algorithm>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

using namespace openpilot::pandad::compatibility;
using Bytes = std::vector<std::uint8_t>;

namespace {
std::size_t assertions = 0;
void check(bool condition, int line) {
  ++assertions;
  if (!condition) throw std::runtime_error("SPI assertion at line " + std::to_string(line));
}
#define CHECK(x) check(static_cast<bool>(x), __LINE__)

std::uint32_t le(const Bytes &bytes, std::size_t offset, unsigned count) {
  std::uint32_t value = 0;
  for (unsigned i = 0; i < count; ++i) value |= static_cast<std::uint32_t>(bytes.at(offset + i)) << (i * 8);
  return value;
}
void append(Bytes &bytes, std::uint32_t value, unsigned count) {
  for (unsigned i = 0; i < count; ++i) bytes.push_back(static_cast<std::uint8_t>(value >> (i * 8)));
}
std::uint32_t crc(const Bytes &bytes) {
  // Test response generation; outbound golden CRC is independently checked by
  // spi_reference_probe using zlib and the pinned original packed structures.
  std::uint32_t value = 0xffffffffU;
  for (auto byte : bytes) {
    value ^= byte;
    for (unsigned bit = 0; bit < 8; ++bit) value = (value >> 1) ^ ((value & 1U) ? 0xedb88320U : 0U);
  }
  return value ^ 0xffffffffU;
}
Bytes response(std::uint32_t id, const Bytes &data, std::size_t wire_size, std::uint8_t status = 0xc3) {
  Bytes bytes{status};
  append(bytes, id, 4);
  append(bytes, static_cast<std::uint32_t>(data.size()), 2);
  bytes.insert(bytes.end(), data.begin(), data.end());
  append(bytes, crc(bytes), 4);
  CHECK(bytes.size() <= wire_size);
  bytes.resize(wire_size);
  return bytes;
}
Result wire_result(Bytes bytes) { return {Status::Ok, bytes.size(), std::move(bytes), true, false}; }
std::string hex(const Bytes &bytes) {
  std::ostringstream output;
  output << std::hex << std::setfill('0');
  for (auto byte : bytes) output << std::setw(2) << static_cast<unsigned>(byte);
  return output.str();
}

class FakeClock final : public Clock {
public:
  std::uint64_t time = 0;
  bool freeze_wait = false;
  std::uint64_t now_us() override { return time; }
  void wait_us(std::uint32_t duration) override { if (!freeze_wait) time += duration; }
};

class FakeSpi final : public SpiIo {
public:
  std::vector<Bytes> requests;
  std::vector<std::uint32_t> timeouts;
  std::function<Result(const Bytes &)> action;
  std::uint32_t id = 0;
  std::size_t exchange_count = 0;
  std::size_t begin_count = 0, end_count = 0;
  Status begin_status = Status::Ok;
  bool lease = false;
  Status begin(std::uint32_t timeout) override {
    ++begin_count;
    CHECK(timeout >= 1 && timeout <= 5);
    lease = begin_status == Status::Ok;
    return begin_status;
  }
  void end() noexcept override { ++end_count; lease = false; }
  Result exchange(const Bytes &tx, std::uint32_t timeout) override {
    ++exchange_count;
    CHECK(lease);
    CHECK(timeout >= 1 && timeout <= 5);
    CHECK(tx.size() <= 4096);
    timeouts.push_back(timeout);
    if (tx[0] != 0xff) {
      requests.push_back(tx);
      id = le(tx, 1, 4);
    }
    if (action) return action(tx);
    if (tx[0] != 0xff) {
      Bytes result(tx.size(), 0);
      result[0] = 0x3c;
      return wire_result(std::move(result));
    }
    return wire_result(response(id, {}, tx.size()));
  }
};
Request read(std::size_t count) {
  Request request;
  request.kind = Kind::BulkRead;
  request.endpoint = 0x81;
  request.read_length = count;
  return request;
}
Request write(std::size_t count) {
  Request request;
  request.kind = Kind::BulkWrite;
  request.endpoint = 3;
  request.payload.resize(count);
  for (std::size_t i = 0; i < count; ++i) request.payload[i] = static_cast<std::uint8_t>(i + 1);
  return request;
}
Request control(bool reading) {
  Request request;
  request.kind = reading ? Kind::ControlRead : Kind::ControlWrite;
  request.request = reading ? 0xdd : 0xdc;
  request.value = 0x1234;
  request.index = 0xabce;
  request.read_length = reading ? 8 : 0;
  return request;
}
void ready_or(FakeSpi &io, std::function<Result(const Bytes &)> poll_action) {
  io.action = [poll_action = std::move(poll_action)](const Bytes &tx) {
    if (tx[0] != 0xff) {
      Bytes data(tx.size(), 0);
      data[0] = 0x3c;
      return wire_result(std::move(data));
    }
    return poll_action(tx);
  };
}

void golden_output() {
  const std::vector<std::pair<std::string, Request>> requests{
    {"control_read", control(true)}, {"control_write", control(false)},
    {"bulk_write", write(3)}, {"bulk_read", read(16)}, {"first_large_read", read(2000)},
  };
  for (const auto &[name, request] : requests) {
    FakeClock clock;
    FakeSpi io;
    SpiBackend backend(io, clock, 0x12345678);
    (void)backend.transact(request, 5);
    CHECK(io.requests.size() == 1);
    std::cout << name << ':' << hex(io.requests.front()) << '\n';
  }
}

void inert_and_validation() {
  FakeClock clock;
  FakeSpi io;
  SpiBackend backend(io, clock, 1);
  CHECK(io.begin_count == 0 && io.exchange_count == 0);
  CHECK(backend.transact(read(16), 0).status == Status::Invalid);
  CHECK(backend.transact(read(16), 6).status == Status::Invalid);
  auto invalid = read(16);
  invalid.endpoint = 1;
  CHECK(backend.transact(invalid, 5).status == Status::Invalid);
  invalid.endpoint = 0x82;
  CHECK(backend.transact(invalid, 5).status == Status::Invalid);
  invalid = write(3);
  invalid.endpoint = 2;
  CHECK(backend.transact(invalid, 5).status == Status::Invalid);
  invalid = control(true);
  invalid.read_length = 4033;
  CHECK(backend.transact(invalid, 5).status == Status::Invalid);
  invalid = control(false);
  invalid.payload = {1};
  CHECK(backend.transact(invalid, 5).status == Status::Invalid);
  CHECK(io.exchange_count == 0);
}

void successful_wire_and_short_read() {
  FakeClock clock;
  FakeSpi io;
  SpiBackend backend(io, clock, 0x12345678);
  ready_or(io, [&](const Bytes &tx) { return wire_result(response(io.id, {1, 2, 3}, tx.size())); });
  auto result = backend.transact(read(16), 5);
  CHECK(result.status == Status::Short && result.actual == 3 && result.data == Bytes({1, 2, 3}));
  CHECK(result.actual_known && !result.delivery_uncertain);
  CHECK(io.requests.size() == 1 && io.requests[0][0] == 0x81);
  CHECK(le(io.requests[0], 7, 2) == 27);
  CHECK(io.begin_count == 1 && io.end_count == 1);
}

void fragmented_bulk_write() {
  FakeClock clock;
  FakeSpi io;
  SpiBackend backend(io, clock, 100);
  auto result = backend.transact(write(9000), 5);
  CHECK(result.status == Status::Ok && result.actual == 9000);
  CHECK(io.requests.size() == 3 && io.exchange_count == 6);
  CHECK(le(io.requests[0], 5, 2) == 4032 && le(io.requests[1], 5, 2) == 4032 && le(io.requests[2], 5, 2) == 936);
  for (std::size_t i = 0; i < io.requests.size(); ++i) {
    CHECK(le(io.requests[i], 1, 4) == 100 + i);
    CHECK(le(io.requests[i], 7, 2) == 11);
  }
  CHECK(io.requests[1][9] == static_cast<std::uint8_t>(4033));
}

void fragmented_large_read() {
  FakeClock clock;
  FakeSpi io;
  SpiBackend backend(io, clock, 200);
  ready_or(io, [&](const Bytes &tx) { return wire_result(response(io.id, Bytes(tx.size() - 11, static_cast<std::uint8_t>(io.id)), tx.size())); });
  auto result = backend.transact(read(10000), 5);
  CHECK(result.status == Status::Ok && result.actual == 10000 && result.data.size() == 10000);
  CHECK(io.requests.size() == 4);
  CHECK(le(io.requests[0], 7, 2) == 1024);
  CHECK(le(io.requests[1], 7, 2) == 4043 && le(io.requests[2], 7, 2) == 4043);
  CHECK(result.data[1012] == 200 && result.data[1013] == 201 && result.data[5045] == 202);
}

void nack_no_replay() {
  FakeClock clock;
  FakeSpi io;
  SpiBackend backend(io, clock, 300);
  ready_or(io, [&](const Bytes &tx) { return wire_result(response(io.id, {}, tx.size(), 0x5a)); });
  auto result = backend.transact(write(9000), 5);
  CHECK(result.status == Status::Busy && result.actual == 0 && result.actual_known && !result.delivery_uncertain);
  CHECK(io.requests.size() == 1 && io.exchange_count == 2);
}

void corrupt_stale_length_no_replay() {
  for (unsigned mode = 0; mode < 4; ++mode) {
    FakeClock clock;
    FakeSpi io;
    SpiBackend backend(io, clock, 400);
    ready_or(io, [&](const Bytes &tx) {
      auto bytes = response(io.id, {}, tx.size());
      if (mode == 0) bytes[7] ^= 1;             // CRC
      if (mode == 1) bytes = response(io.id - 1, {}, tx.size());
      if (mode == 2) { bytes[5] = 1; }         // beyond write max_rx=0
      if (mode == 3) bytes[0] = 0x3c;          // ready again; no implicit retry
      return wire_result(std::move(bytes));
    });
    auto result = backend.transact(write(3), 5);
    CHECK(result.status == Status::IoError && !result.actual_known && result.delivery_uncertain);
    CHECK(io.requests.size() == 1 && io.exchange_count == 2);
  }
}

void low_level_partial_and_disconnect() {
  for (unsigned mode = 0; mode < 3; ++mode) {
    FakeClock clock;
    FakeSpi io;
    SpiBackend backend(io, clock, 500);
    ready_or(io, [&](const Bytes &tx) {
      if (mode == 0) return Result{Status::Short, 3, Bytes(3), true, false};
      if (mode == 1) return Result{Status::Disconnected, 0, {}, false, false};
      return Result{Status::Ok, tx.size() - 1, Bytes(tx.size() - 1), true, false};
    });
    auto result = backend.transact(read(10), 5);
    CHECK(result.status == (mode == 1 ? Status::Disconnected : Status::IoError));
    CHECK(result.actual == 0 && !result.actual_known && result.data.empty());
    CHECK(io.requests.size() == 1);
  }
}

void partial_write_progress_preserved() {
  FakeClock clock;
  FakeSpi io;
  SpiBackend backend(io, clock, 600);
  ready_or(io, [&](const Bytes &tx) {
    if (io.id == 601) return Result{Status::Timeout, 0, {}, false, false};
    return wire_result(response(io.id, {}, tx.size()));
  });
  const auto result = backend.transact(write(9000), 5);
  CHECK(result.status == Status::Timeout && result.actual == 4032);
  CHECK(!result.actual_known && result.delivery_uncertain && io.requests.size() == 2);
}

void partial_read_progress_preserved() {
  FakeClock clock;
  FakeSpi io;
  SpiBackend backend(io, clock, 650);
  ready_or(io, [&](const Bytes &tx) {
    if (io.id == 651) return Result{Status::Disconnected, 0, {}, false, false};
    return wire_result(response(io.id, Bytes(tx.size() - 11, 42), tx.size()));
  });
  const auto result = backend.transact(read(9000), 5);
  CHECK(result.status == Status::Disconnected && result.actual == 1013 && result.data == Bytes(1013, 42));
  CHECK(!result.actual_known && io.requests.size() == 2);
}

void bounded_poll_timeout_and_wrap() {
  for (auto start : {std::uint64_t{0}, std::numeric_limits<std::uint64_t>::max() - 500}) {
    FakeClock clock;
    clock.time = start;
    FakeSpi io;
    SpiBackend backend(io, clock, 700);
    ready_or(io, [](const Bytes &tx) { return wire_result(Bytes(tx.size(), 0)); });
    auto result = backend.transact(read(10), 1);
    CHECK(result.status == Status::Timeout && !result.actual_known);
    // A <1ms remainder cannot be rounded up into another 1ms kernel call.
    CHECK(clock.time - start == 100 && io.exchange_count == 2 && io.requests.size() == 1);
    CHECK(io.end_count == 1);
  }
  FakeClock frozen;
  frozen.freeze_wait = true;
  FakeSpi io;
  SpiBackend backend(io, frozen, 750);
  ready_or(io, [](const Bytes &tx) { return wire_result(Bytes(tx.size(), 0)); });
  CHECK(backend.transact(read(10), 5).status == Status::Timeout);
  CHECK(io.exchange_count == kSpiMaxPolls + 1 && io.requests.size() == 1);
}

void cancel_boundaries() {
  FakeClock clock;
  FakeSpi io;
  SpiBackend backend(io, clock, 800);
  io.action = [&](const Bytes &tx) {
    Bytes bytes(tx.size()); bytes[0] = 0x3c;
    backend.cancel();
    return wire_result(std::move(bytes));
  };
  auto result = backend.transact(write(3), 5);
  CHECK(result.status == Status::Cancelled && result.delivery_uncertain && !result.actual_known);
  CHECK(io.exchange_count == 1 && io.end_count == 1);
  result = backend.transact(read(10), 5);
  CHECK(result.status == Status::Cancelled && result.actual_known && io.exchange_count == 1);
}

void deadline_before_next_request() {
  for (auto tick : {999U, 1000U, 1001U}) {
    FakeClock clock;
    FakeSpi io;
    SpiBackend backend(io, clock, 850);
    ready_or(io, [&](const Bytes &tx) {
      clock.time = tick;
      return wire_result(response(io.id, {}, tx.size()));
    });
    const auto result = backend.transact(write(5000), 1);
    CHECK(result.actual == 4032);
    CHECK(result.status == Status::Timeout);
    CHECK(result.actual_known && !result.delivery_uncertain);
    CHECK(io.requests.size() == 1);
  }
}

void isolated_backends_and_bus_busy() {
  FakeClock clock_a, clock_b;
  FakeSpi io_a, io_b;
  SpiBackend a(io_a, clock_a, 900), b(io_b, clock_b, 1000);
  ready_or(io_a, [&](const Bytes &tx) {
    const auto result = b.transact(write(3), 5);
    CHECK(result.status == Status::Ok && result.actual == 3);
    a.cancel();
    return wire_result(Bytes(tx.size(), 0));
  });
  const auto result = a.transact(read(10), 5);
  CHECK(result.status == Status::Cancelled);
  CHECK(io_a.requests.size() == 1 && io_b.requests.size() == 1);
  CHECK(le(io_a.requests[0], 1, 4) == 900 && le(io_b.requests[0], 1, 4) == 1000);
  CHECK(io_a.end_count == 1 && io_b.end_count == 1);
  io_b.begin_status = Status::Busy;
  const auto busy = b.transact(write(3), 5);
  CHECK(busy.status == Status::Busy && busy.actual_known && !busy.delivery_uncertain);
  CHECK(io_b.exchange_count == 2);
}

void id_exhaustion() {
  FakeClock clock;
  FakeSpi io;
  SpiBackend backend(io, clock, std::numeric_limits<std::uint32_t>::max());
  CHECK(backend.transact(write(3), 5).status == Status::Ok);
  auto result = backend.transact(write(3), 5);
  CHECK(result.status == Status::Poisoned && result.actual_known && !result.delivery_uncertain);
  CHECK(io.requests.size() == 1 && io.exchange_count == 2);
}

void wrapper_does_not_retry_consuming_read() {
  FakeClock clock;
  FakeSpi io;
  SpiBackend backend(io, clock, 1100);
  ready_or(io, [](const Bytes &) { return Result{Status::Timeout, 0, {}, false, false}; });
  Transport transport(backend, clock);
  const auto result = transport.perform(read(10), 100);
  CHECK(result.status == Status::Timeout && !result.actual_known && io.requests.size() == 1);
  CHECK(io.exchange_count == 2);
}

void wrapper_preserves_partial_prefix() {
  for (bool reading : {false, true}) {
    FakeClock clock;
    FakeSpi io;
    SpiBackend backend(io, clock, 1200);
    ready_or(io, [&](const Bytes &tx) {
      if (io.id == 1201) return Result{Status::Timeout, 0, {}, false, false};
      return wire_result(response(io.id, reading ? Bytes(tx.size() - 11, 7) : Bytes{}, tx.size()));
    });
    Transport transport(backend, clock);
    const auto result = transport.perform(reading ? read(9000) : write(9000), 100);
    CHECK(result.status == Status::Timeout && !result.actual_known);
    CHECK(result.actual == (reading ? 1013 : 4032));
    CHECK(result.data.size() == (reading ? 1013 : 0));
    CHECK(io.requests.size() == 2 && io.exchange_count == 4);
  }
}
}  // namespace

int main(int argc, char **argv) {
  try {
    if (argc == 2 && std::string(argv[1]) == "--golden") { golden_output(); return 0; }
    const std::vector<std::pair<const char *, std::function<void()>>> groups{
      {"inert_and_validation", inert_and_validation}, {"successful_wire_and_short_read", successful_wire_and_short_read},
      {"fragmented_bulk_write", fragmented_bulk_write}, {"fragmented_large_read", fragmented_large_read},
      {"nack_no_replay", nack_no_replay}, {"corrupt_stale_length_no_replay", corrupt_stale_length_no_replay},
      {"low_level_partial_and_disconnect", low_level_partial_and_disconnect}, {"partial_write_progress_preserved", partial_write_progress_preserved},
      {"partial_read_progress_preserved", partial_read_progress_preserved}, {"bounded_poll_timeout_and_wrap", bounded_poll_timeout_and_wrap},
      {"cancel_boundaries", cancel_boundaries}, {"deadline_before_next_request", deadline_before_next_request},
      {"isolated_backends_and_bus_busy", isolated_backends_and_bus_busy}, {"id_exhaustion", id_exhaustion},
      {"wrapper_does_not_retry_consuming_read", wrapper_does_not_retry_consuming_read},
      {"wrapper_preserves_partial_prefix", wrapper_preserves_partial_prefix},
    };
    for (const auto &[name, test] : groups) { test(); std::cout << "PASS " << name << '\n'; }
    std::cout << "{\"suite\":\"spi_backend\",\"groups_passed\":" << groups.size()
              << ",\"assertions\":" << assertions << ",\"failures\":0,\"skipped\":0,\"hardware_access\":false}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
