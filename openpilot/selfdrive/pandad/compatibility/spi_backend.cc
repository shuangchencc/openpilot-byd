#include "spi_backend.h"

#include <algorithm>
#include <limits>

namespace openpilot::pandad::compatibility {
namespace {
constexpr std::uint8_t kReady = 0x3c, kAck = 0xc3, kNack = 0x5a, kPoll = 0xff;
using Bytes = std::vector<std::uint8_t>;

void put_le(Bytes &bytes, std::uint32_t value, unsigned count) {
  for (unsigned i = 0; i < count; ++i) bytes.push_back(static_cast<std::uint8_t>(value >> (i * 8)));
}
std::uint32_t get_le(const Bytes &bytes, std::size_t offset, unsigned count) {
  std::uint32_t value = 0;
  for (unsigned i = 0; i < count; ++i) value |= static_cast<std::uint32_t>(bytes[offset + i]) << (i * 8);
  return value;
}
std::uint32_t crc32(const Bytes &bytes, std::size_t count) {
  std::uint32_t crc = 0xffffffffU;
  for (std::size_t i = 0; i < count; ++i) {
    crc ^= bytes[i];
    for (unsigned bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ ((crc & 1U) ? 0xedb88320U : 0U);
  }
  return crc ^ 0xffffffffU;
}
Result failure(Status status, bool sent, bool write) {
  return {status, 0, {}, !sent, sent && write};
}
struct BusLease {
  SpiIo &io;
  ~BusLease() { io.end(); }
};
}  // namespace

Status SpiBackend::boundary(std::uint64_t start_us, std::uint64_t budget_us) {
  if (cancelled_.load()) return Status::Cancelled;
  // Unsigned elapsed subtraction also covers the clock wrapping at UINT64_MAX.
  return clock_.now_us() - start_us >= budget_us ? Status::Timeout : Status::Ok;
}

std::uint32_t SpiBackend::remaining_ms(std::uint64_t start_us, std::uint64_t budget_us) {
  const auto elapsed = clock_.now_us() - start_us;
  if (elapsed >= budget_us) return 0;
  return static_cast<std::uint32_t>((budget_us - elapsed) / 1000);
}

Result SpiBackend::transfer(std::uint8_t endpoint, const Bytes &tx, std::size_t max_rx,
                            bool write, std::uint64_t start_us, std::uint64_t budget_us) {
  auto status = boundary(start_us, budget_us);
  if (status != Status::Ok) return failure(status, false, write);
  if (remaining_ms(start_us, budget_us) == 0) return failure(Status::Timeout, false, write);
  if (next_id_ > std::numeric_limits<std::uint32_t>::max()) return failure(Status::Poisoned, false, write);
  const auto id = static_cast<std::uint32_t>(next_id_++);
  const auto response_size = kSpiResponseHeaderBytes + max_rx + 4;
  Bytes wire{endpoint};
  put_le(wire, id, 4);
  put_le(wire, static_cast<std::uint32_t>(tx.size()), 2);
  put_le(wire, static_cast<std::uint32_t>(response_size), 2);
  wire.insert(wire.end(), tx.begin(), tx.end());
  put_le(wire, crc32(wire, wire.size()), 4);

  // This request is emitted once, including when the kernel reports a timeout.
  // Reads can consume the MCU CAN queue too; uncertain reads are NOT retryable.
  status = boundary(start_us, budget_us);
  if (status != Status::Ok) return failure(status, false, write);
  const auto request_timeout = remaining_ms(start_us, budget_us);
  if (request_timeout == 0) return failure(Status::Timeout, false, write);
  auto reply = io_.exchange(wire, request_timeout);
  if (reply.status != Status::Ok) return failure(reply.status == Status::Short ? Status::IoError : reply.status, true, write);
  if (!reply.actual_known || reply.actual != wire.size() || reply.data.size() != wire.size()) {
    return failure(Status::IoError, true, write);
  }
  if (reply.data[0] != kReady) return failure(Status::IoError, true, write);

  const Bytes poll(response_size, kPoll);
  for (std::uint32_t attempt = 0; attempt < kSpiMaxPolls; ++attempt) {
    status = boundary(start_us, budget_us);
    if (status != Status::Ok) return failure(status, true, write);
    const auto poll_timeout = remaining_ms(start_us, budget_us);
    if (poll_timeout == 0) return failure(Status::Timeout, true, write);
    reply = io_.exchange(poll, poll_timeout);
    if (reply.status != Status::Ok) return failure(reply.status == Status::Short ? Status::IoError : reply.status, true, write);
    if (!reply.actual_known || reply.actual != poll.size() || reply.data.size() != poll.size()) {
      return failure(Status::IoError, true, write);
    }
    const auto &response = reply.data;
    if (response[0] == kReady) return failure(Status::IoError, true, write);
    if (response[0] == kAck || response[0] == kNack) {
      const auto length = get_le(response, 5, 2);
      if (length > max_rx || get_le(response, 1, 4) != id ||
          get_le(response, kSpiResponseHeaderBytes + length, 4) != crc32(response, kSpiResponseHeaderBytes + length)) {
        return failure(Status::IoError, true, write);
      }
      // NACK explicitly refuses the entire request in the locked firmware.
      // Never issue a new ID or retry it automatically under backpressure.
      if (response[0] == kNack) {
        if (length != 0) return failure(Status::IoError, true, write);
        return {Status::Busy, 0, {}, true, false};
      }
      if (write && length != 0) return failure(Status::IoError, true, true);
      Bytes data(response.begin() + kSpiResponseHeaderBytes, response.begin() + kSpiResponseHeaderBytes + length);
      // Preserve completion after a late syscall as known progress; the outer
      // wrapper reports deadline/cancel status without hiding the actual count.
      return {length < max_rx ? Status::Short : Status::Ok, length, std::move(data), true, false};
    }
    const auto elapsed = clock_.now_us() - start_us;
    if (elapsed >= budget_us) return failure(Status::Timeout, true, write);
    clock_.wait_us(static_cast<std::uint32_t>(std::min<std::uint64_t>(kSpiPollWaitUs, budget_us - elapsed)));
  }
  return failure(Status::Timeout, true, write);
}

Result SpiBackend::transact(const Request &request, std::uint32_t timeout_ms) {
  if (!valid_request(request) || timeout_ms == 0 || timeout_ms > kBackendSliceMs ||
      (request.kind == Kind::BulkRead && request.endpoint != 0x81) ||
      (request.kind == Kind::BulkWrite && request.endpoint != 0x03) ||
      ((request.kind == Kind::ControlRead || request.kind == Kind::ControlWrite) &&
       (request.read_length > kSpiPayloadBytes || !request.payload.empty()))) {
    return failure(Status::Invalid, false, false);
  }
  const auto start_us = clock_.now_us();
  const auto budget_us = static_cast<std::uint64_t>(timeout_ms) * 1000;
  auto status = boundary(start_us, budget_us);
  if (status != Status::Ok) return failure(status, false, !is_read(request.kind));
  if ((request.kind == Kind::BulkRead && request.read_length == 0) ||
      (request.kind == Kind::BulkWrite && request.payload.empty())) return {Status::Ok, 0, {}, true, false};
  status = io_.begin(timeout_ms);
  if (status != Status::Ok) return failure(status, false, !is_read(request.kind));
  const BusLease lease{io_};

  if (request.kind == Kind::ControlRead || request.kind == Kind::ControlWrite) {
    Bytes packet{static_cast<std::uint8_t>(request.request)};
    put_le(packet, request.value, 2);
    put_le(packet, request.index, 2);
    put_le(packet, static_cast<std::uint32_t>(request.read_length), 2);
    return transfer(0, packet, request.read_length, request.kind == Kind::ControlWrite, start_us, budget_us);
  }

  Result total{Status::Ok, 0, {}, true, false};
  const bool read = request.kind == Kind::BulkRead;
  const auto requested = read ? request.read_length : request.payload.size();
  while (total.actual < requested) {
    const auto chunk_size = std::min(requested - total.actual, read && total.actual == 0 ? kSpiFirstReadBytes : kSpiPayloadBytes);
    Bytes payload;
    if (!read) payload.assign(request.payload.begin() + total.actual, request.payload.begin() + total.actual + chunk_size);
    auto part = transfer(static_cast<std::uint8_t>(request.endpoint), payload, read ? chunk_size : 0,
                         !read, start_us, budget_us);
    total.status = part.status;
    total.actual_known = part.actual_known;
    total.delivery_uncertain = part.delivery_uncertain;
    if (read) {
      total.actual += part.actual;
      total.data.insert(total.data.end(), part.data.begin(), part.data.end());
    } else if (part.status == Status::Ok) {
      total.actual += chunk_size;
    }
    if (part.status != Status::Ok) return total;
  }
  return total;
}

}  // namespace openpilot::pandad::compatibility
