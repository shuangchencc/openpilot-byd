#include "../usb_backend.h"
#include "../can_codec.h"

#include <algorithm>
#include <atomic>
#include <cassert>
#include <condition_variable>
#include <deque>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

using namespace openpilot::pandad::compatibility;
namespace {
#define CHECK(x) do { if (!(x)) throw std::runtime_error(std::string(__func__) + ": " + #x); } while (false)
struct Transfer {
  bool control = false;
  int result = 0;
  int actual = 0;
  std::vector<std::uint8_t> bytes;
};
struct State {
  int initializes = 0, enumerations = 0, opens = 0, serials = 0, claims = 0;
  int releases = 0, closes = 0, shutdowns = 0, transfers = 0;
  int initialize_error = 0, enumerate_error = 0, open_error = 0;
  int serial_error = 0, claim_error = 0;
  std::vector<UsbDeviceInfo> devices;
  std::map<UsbDeviceId, std::string> serial_by_id;
  std::deque<Transfer> replies;
  std::vector<unsigned int> endpoints, request_types, requests, values, indices, timeouts;
  std::vector<std::vector<std::uint8_t>> output;
  std::function<void()> in_transfer;
};
class FakeApi final : public LibusbApi {
public:
  explicit FakeApi(std::shared_ptr<State> state) : state_(std::move(state)) {}
  int initialize() override { ++state_->initializes; return state_->initialize_error; }
  int enumerate(std::vector<UsbDeviceInfo> &devices) override {
    ++state_->enumerations; devices = state_->devices; return state_->enumerate_error;
  }
  int open(UsbDeviceId device, UsbHandle &handle) override {
    ++state_->opens; handle = state_->open_error == 0 ? device : 0; return state_->open_error;
  }
  int serial(UsbHandle handle, std::uint8_t, std::string &value, std::uint32_t timeout) override {
    CHECK(timeout > 0 && timeout <= 5);
    ++state_->serials; value = state_->serial_by_id[handle]; return state_->serial_error;
  }
  int claim(UsbHandle, int interface_number) override {
    CHECK(interface_number == 0); ++state_->claims; return state_->claim_error;
  }
  int control(UsbHandle, std::uint8_t type, std::uint8_t request, std::uint16_t value, std::uint16_t index,
              std::uint8_t *data, std::uint16_t length, std::uint32_t timeout) override {
    state_->request_types.push_back(type); state_->requests.push_back(request);
    state_->values.push_back(value); state_->indices.push_back(index);
    auto reply = transfer(true, (type & 0x80) != 0, 0, data, length, timeout);
    return reply.result;
  }
  int bulk(UsbHandle, std::uint8_t endpoint, std::uint8_t *data, int length,
           int &actual, std::uint32_t timeout) override {
    auto reply = transfer(false, (endpoint & 0x80) != 0, endpoint, data, length, timeout);
    actual = reply.actual; return reply.result;
  }
  void release(UsbHandle, int interface_number) override { CHECK(interface_number == 0); ++state_->releases; }
  void close(UsbHandle) override { ++state_->closes; }
  void shutdown() override { ++state_->shutdowns; }
private:
  Transfer transfer(bool control, bool read, unsigned int endpoint, std::uint8_t *data,
                    int length, std::uint32_t timeout) {
    CHECK(timeout > 0 && timeout <= 5 && !state_->replies.empty());
    ++state_->transfers; state_->endpoints.push_back(endpoint); state_->timeouts.push_back(timeout);
    if (!read) {
      if (length == 0) state_->output.emplace_back();
      else state_->output.emplace_back(data, data + length);
    }
    auto reply = state_->replies.front(); state_->replies.pop_front();
    CHECK(reply.control == control);
    if (read && !reply.bytes.empty()) {
      CHECK(reply.bytes.size() <= static_cast<std::size_t>(length));
      std::copy(reply.bytes.begin(), reply.bytes.end(), data);
    }
    if (state_->in_transfer) state_->in_transfer();
    return reply;
  }
  std::shared_ptr<State> state_;
};
UsbDeviceInfo device(UsbDeviceId id = 1, std::uint16_t board_type = 6) {
  return {id, 0xbbaa, 0xddcc, static_cast<std::uint16_t>(board_type << 8), 3, true,
          {{0, 0, {{0x81, 2, 64}, {0x02, 2, 64}, {0x03, 2, 64}}},
           {0, 1, {{0x81, 3, 64}, {0x02, 2, 64}, {0x03, 2, 64}}}}};
}
std::shared_ptr<State> state() {
  auto result = std::make_shared<State>();
  result->devices = {device()}; result->serial_by_id[1] = "fixture:DOS-A"; return result;
}
UsbBackend backend(const std::shared_ptr<State> &state) { return UsbBackend(std::make_unique<FakeApi>(state)); }
void open(UsbBackend &usb) { CHECK(usb.open_bound({"fixture:DOS-A", 6, true}, true) == Status::Ok); }
Request read(std::size_t count = 8) { Request r; r.kind = Kind::BulkRead; r.endpoint = 0x81; r.read_length = count; return r; }
Request write() { Request r; r.kind = Kind::BulkWrite; r.endpoint = 0x03; r.payload = {1, 2, 3, 4}; return r; }
class FakeClock final : public Clock {
public:
  std::uint64_t now_us() override { return now; }
  void wait_us(std::uint32_t duration) override { now += duration; }
  std::uint64_t now = 0;
};
void inert_construction_and_permission() {
  auto s = state();
  { auto usb = backend(s); CHECK(!usb.is_open()); CHECK(usb.open_bound({"fixture:DOS-A", 6, true}) == Status::Invalid);
    CHECK(usb.open_bound({}, true) == Status::Invalid); CHECK(usb.open_bound({"fixture:DOS-A", 0}, true) == Status::Invalid);
    CHECK(usb.open_bound({" fixture:DOS-A", 6}, true) == Status::Invalid);
    CHECK(usb.transact(read(), 5).status == Status::Disconnected); }
  CHECK(s->initializes == 0 && s->opens == 0 && s->shutdowns == 0 && s->transfers == 0);
  // Links the production implementation but executes no initialize/list/open.
  { UsbBackend production; CHECK(!production.is_open()); CHECK(production.open_bound({}) == Status::Invalid); }
}
void selection_and_cleanup() {
  auto s = state(); s->devices.insert(s->devices.begin(), device(2, 7)); s->serial_by_id[2] = "fixture:RED-B";
  s->devices[1].vendor = 0x3801;
  { auto usb = backend(s); open(usb); CHECK(usb.is_open()); CHECK(s->opens == 2 && s->closes == 1 && s->claims == 1);
    CHECK(usb.open_bound({"fixture:DOS-A", 6, true}, true) == Status::Busy); }
  CHECK(s->releases == 1 && s->closes == 2 && s->shutdowns == 1);
}
void reversed_selection() {
  for (bool reverse : {false, true}) {
    auto s = state(); s->devices.push_back(device(2, 7)); s->serial_by_id[2] = "fixture:RED-B";
    if (reverse) std::reverse(s->devices.begin(), s->devices.end());
    auto usb = backend(s); open(usb); CHECK(usb.is_open() && s->claims == 1);
  }
}
void reject_bootstub_and_dfu() {
  auto s = state(); s->devices[0].product = 0xddee;
  auto dfu = device(2); dfu.vendor = 0x0483; dfu.product = 0xdf11; s->devices.push_back(dfu);
  auto usb = backend(s); CHECK(usb.open_bound({"fixture:DOS-A", 6, true}, true) == Status::Disconnected);
  CHECK(s->opens == 0 && s->claims == 0 && s->shutdowns == 1);
}
void reject_duplicate_and_replacement() {
  for (bool duplicate : {true, false}) {
    auto s = state();
    if (duplicate) { s->devices.push_back(device(2)); s->serial_by_id[2] = "fixture:DOS-A"; }
    else s->devices[0].bcd_device = 0x700;
    auto usb = backend(s); CHECK(usb.open_bound({"fixture:DOS-A", 6, true}, true) == Status::Poisoned);
    CHECK(!usb.is_open() && s->claims == 0 && s->closes == s->opens && s->shutdowns == 1);
  }
}
void malformed_descriptors() {
  for (int variant = 0; variant < 9; ++variant) {
    auto s = state(); auto &d = s->devices[0];
    switch (variant) {
      case 0: d.active_configuration_known = false; break;
      case 1: d.serial_index = 0; break;
      case 2: d.alternates[0].interface_number = 1; break;
      case 3: d.alternates[0].endpoints[0].attributes = 3; break;
      case 4: d.alternates[0].endpoints[0].max_packet_size = 512; break;
      case 5: d.alternates[0].endpoints.pop_back(); break;
      case 6: d.alternates[0].endpoints.push_back(d.alternates[0].endpoints[0]); break;
      case 7: d.alternates.push_back(d.alternates[0]); break;
      case 8: d.bcd_device = 0x601; break;
    }
    auto usb = backend(s); CHECK(usb.open_bound({"fixture:DOS-A", 6, true}, true) == Status::Poisoned);
    CHECK(s->claims == 0 && s->closes == 1 && s->shutdowns == 1);
  }
}
void active_alternate_needs_explicit_external_verification() {
  auto s = state(); auto usb = backend(s);
  CHECK(usb.open_bound({"fixture:DOS-A", 6}, true) == Status::Invalid);
  CHECK(s->initializes == 0 && s->opens == 0 && s->claims == 0);
}
void open_failures_cleanup() {
  for (int phase = 0; phase < 5; ++phase) {
    auto s = state();
    switch (phase) {
      case 0: s->initialize_error = kUsbIo; break;
      case 1: s->enumerate_error = kUsbIo; break;
      case 2: s->open_error = kUsbAccess; break;
      case 3: s->serial_error = kUsbTimeout; break;
      case 4: s->claim_error = kUsbBusy; break;
    }
    { auto usb = backend(s); CHECK(usb.open_bound({"fixture:DOS-A", 6, true}, true) != Status::Ok); CHECK(!usb.is_open()); }
    CHECK(s->shutdowns == (phase == 0 ? 0 : 1));
    CHECK(s->closes == (phase >= 3 ? 1 : 0));
    CHECK(s->releases == 0);
  }
}
void bulk_short_read_and_write() {
  auto s = state(); auto usb = backend(s); open(usb);
  s->replies.push_back({false, 0, 3, {9, 8, 7}}); auto r = usb.transact(read(), 5);
  CHECK(r.status == Status::Short && r.actual == 3 && r.data == std::vector<std::uint8_t>({9, 8, 7}) && r.actual_known);
  s->replies.push_back({false, 0, 2, {}}); r = usb.transact(write(), 5);
  CHECK(r.status == Status::Short && r.actual == 2 && r.delivery_uncertain && r.data.empty());
  CHECK(s->transfers == 2 && s->output == std::vector<std::vector<std::uint8_t>>({{1, 2, 3, 4}}));
}
void partial_timeout_preserves_data() {
  auto s = state(); auto usb = backend(s); open(usb);
  s->replies.push_back({false, kUsbTimeout, 2, {7, 8}}); auto r = usb.transact(read(), 2);
  CHECK(r.status == Status::Timeout && r.actual == 2 && r.actual_known && r.data == std::vector<std::uint8_t>({7, 8}));
  CHECK(!r.delivery_uncertain && s->timeouts == std::vector<unsigned int>({2}));
}
void control_read_write_and_unknown_count() {
  auto s = state(); auto usb = backend(s); open(usb);
  Request r; r.kind = Kind::ControlRead; r.request = 0xdd; r.value = 0xabcd; r.index = 0x4321; r.read_length = 8;
  s->replies.push_back({true, 3, 0, {1, 2, 3}}); auto result = usb.transact(r, 5);
  CHECK(result.status == Status::Short && result.actual == 3 && result.data == std::vector<std::uint8_t>({1, 2, 3}));
  r.kind = Kind::ControlWrite; r.read_length = 0; r.payload = {1, 2};
  s->replies.push_back({true, kUsbTimeout, 0, {}}); result = usb.transact(r, 5);
  CHECK(result.status == Status::Timeout && !result.actual_known && result.delivery_uncertain && result.actual == 0);
  s->replies.push_back({true, 2, 0, {}}); result = usb.transact(r, 5);
  CHECK(result.status == Status::Ok && result.actual == 2 && !result.delivery_uncertain);
  r.payload.clear(); s->replies.push_back({true, 0, 0, {}}); result = usb.transact(r, 5);
  CHECK(result.status == Status::Ok && result.actual == 0 && result.actual_known && !result.delivery_uncertain);
  r.kind = Kind::ControlRead; r.read_length = 8; s->replies.push_back({true, kUsbTimeout, 0, {}});
  result = usb.transact(r, 5);
  CHECK(result.status == Status::Timeout && result.actual == 0 && !result.actual_known && !result.delivery_uncertain);
  CHECK(s->request_types == std::vector<unsigned int>({0xc0, 0x40, 0x40, 0x40, 0xc0}));
  CHECK(s->requests == std::vector<unsigned int>(5, 0xdd));
  CHECK(s->values == std::vector<unsigned int>(5, 0xabcd) && s->indices == std::vector<unsigned int>(5, 0x4321));
}
void error_classes_and_no_replay() {
  const std::pair<int, Status> errors[] = {{kUsbTimeout, Status::Timeout}, {kUsbNoDevice, Status::Disconnected},
    {kUsbPipe, Status::Stall}, {kUsbInterrupted, Status::Cancelled}, {kUsbBusy, Status::Busy},
    {kUsbOverflow, Status::Poisoned}, {kUsbIo, Status::IoError}};
  for (const auto &[code, expected] : errors) {
    auto s = state(); auto usb = backend(s); open(usb); FakeClock clock; Transport transport(usb, clock);
    s->replies.push_back({false, code, 2, {}}); auto result = transport.perform(write());
    CHECK(result.status == expected && result.delivery_uncertain);
    if (code == kUsbOverflow) CHECK(result.actual == 0 && !result.actual_known && result.data.empty());
    else CHECK(result.actual == 2 && result.actual_known);
    CHECK(s->transfers == 1);
  }
}
void invalid_requests_never_touch_api() {
  auto s = state(); auto usb = backend(s); open(usb);
  auto r = read(); r.endpoint = 0x82; CHECK(usb.transact(r, 5).status == Status::Invalid);
  r = write(); r.endpoint = 0x02; CHECK(usb.transact(r, 5).status == Status::Invalid);
  CHECK(usb.transact(read(), 0).status == Status::Invalid); CHECK(usb.transact(read(), 6).status == Status::Invalid);
  r = read(kMaxBulkBytes + 1); CHECK(usb.transact(r, 5).status == Status::Invalid);
  CHECK(s->transfers == 0);
}
void impossible_actual_poisoned() {
  {
    auto s = state(); auto usb = backend(s); open(usb);
    s->replies.push_back({false, kUsbOverflow, 2, {1, 2}});
    auto result = usb.transact(read(), 5);
    CHECK(result.status == Status::Poisoned && !result.actual_known && result.actual == 0 && result.data.empty());
  }
  for (int actual : {-1, 9}) {
    auto s = state(); auto usb = backend(s); open(usb); s->replies.push_back({false, 0, actual, {}});
    auto result = usb.transact(read(), 5); CHECK(result.status == Status::Poisoned && !result.actual_known && result.data.empty());
  }
}
void stream_fragments_at_usb_packet_boundary() {
  auto s = state(); auto usb = backend(s); open(usb); CanStreamParser parser(CanCapability::CanFd);
  CanFrame frame; frame.address = 0x123; frame.fd = true; frame.payload.resize(64, 0xaa);
  auto encoded = encode_can_frame(frame, CanCapability::CanFd, FrameDirection::Rx); CHECK(encoded && encoded.value.size() == 70);
  s->replies.push_back({false, 0, 64, {encoded.value.begin(), encoded.value.begin() + 64}});
  s->replies.push_back({false, kUsbTimeout, 6, {encoded.value.begin() + 64, encoded.value.end()}});
  auto a = usb.transact(read(128), 5); auto first = parser.feed(a.data.data(), a.data.size()); CHECK(first && first.value.empty());
  auto b = usb.transact(read(128), 5); auto second = parser.feed(b.data.data(), b.data.size());
  CHECK(b.status == Status::Timeout && second && second.value.size() == 1 && second.value[0].payload == frame.payload);
  CHECK(s->transfers == 2 && parser.buffered_size() == 0);
}
void two_session_parsers_do_not_mix_and_peer_survives_disconnect() {
  auto a = state(), b = state(); auto usb_a = backend(a), usb_b = backend(b); open(usb_a); open(usb_b);
  FakeClock clock_a, clock_b; Transport session_a(usb_a, clock_a), session_b(usb_b, clock_b);
  CanStreamParser parser_a(CanCapability::ClassicOnly), parser_b(CanCapability::ClassicOnly);
  CanFrame first; first.address = 0x123; first.payload = {1, 2, 3, 4};
  CanFrame second; second.address = 0x456; second.payload = {5, 6, 7, 8};
  auto encoded_a = encode_can_frame(first, CanCapability::ClassicOnly, FrameDirection::Rx);
  auto encoded_b = encode_can_frame(second, CanCapability::ClassicOnly, FrameDirection::Rx);
  CHECK(encoded_a && encoded_b);
  a->replies.push_back({false, 0, 4, {encoded_a.value.begin(), encoded_a.value.begin() + 4}});
  b->replies.push_back({false, 0, 6, {encoded_b.value.begin(), encoded_b.value.begin() + 6}});
  b->replies.push_back({false, 0, 4, {encoded_b.value.begin() + 6, encoded_b.value.end()}});
  a->replies.push_back({false, kUsbNoDevice, 0, {}});
  auto first_a = session_a.perform(read()); auto a_prefix = parser_a.feed(first_a.data.data(), first_a.data.size());
  auto first_b = session_b.perform(read()); auto b_prefix = parser_b.feed(first_b.data.data(), first_b.data.size());
  CHECK(a_prefix && a_prefix.value.empty() && b_prefix && b_prefix.value.empty());
  CHECK(session_a.perform(read()).status == Status::Disconnected);
  auto last_b = session_b.perform(read()); auto complete_b = parser_b.feed(last_b.data.data(), last_b.data.size());
  CHECK(complete_b && complete_b.value.size() == 1 && complete_b.value[0].address == second.address);
  CHECK(complete_b.value[0].payload == second.payload && parser_a.buffered_size() == 4 && parser_b.buffered_size() == 0);
  CHECK(session_b.healthy() && !session_a.healthy());
  CHECK(session_a.perform(read()).status == Status::Poisoned && a->transfers == 2 && b->transfers == 2);
}
void cancelled_read_does_not_retry() {
  auto s = state(); auto usb = backend(s); open(usb); FakeClock clock; Transport transport(usb, clock);
  s->replies.push_back({false, kUsbTimeout, 2, {9, 8}});
  s->in_transfer = [&] { transport.cancel(); };
  auto result = transport.perform(read());
  CHECK(result.status == Status::Cancelled && result.actual == 2 && result.data == std::vector<std::uint8_t>({9, 8}));
  CHECK(s->transfers == 1); CHECK(transport.perform(read()).status == Status::Cancelled && s->transfers == 1);
}
void separate_handles_progress_during_blocked_peer() {
  auto a = state(), b = state(); auto usb_a = backend(a), usb_b = backend(b); open(usb_a); open(usb_b);
  a->replies.push_back({false, kUsbTimeout, 0, {}}); b->replies.push_back({false, 0, 1, {42}});
  std::mutex mutex; std::condition_variable cv; bool entered = false, finish = false;
  a->in_transfer = [&] { std::unique_lock<std::mutex> lock(mutex); entered = true; cv.notify_all(); cv.wait(lock, [&] { return finish; }); };
  Result first; std::thread thread([&] { first = usb_a.transact(read(1), 5); });
  { std::unique_lock<std::mutex> lock(mutex); cv.wait(lock, [&] { return entered; }); }
  auto second = usb_b.transact(read(1), 5);
  CHECK(second.status == Status::Ok && second.data == std::vector<std::uint8_t>({42}));
  CHECK(usb_a.transact(read(), 5).status == Status::Busy);
  { std::lock_guard<std::mutex> lock(mutex); finish = true; } cv.notify_all(); thread.join();
  CHECK(first.status == Status::Timeout && a->transfers == 1 && b->transfers == 1);
}
void close_does_not_reuse_session() {
  auto s = state(); auto usb = backend(s); open(usb); usb.close(); usb.close();
  CHECK(usb.transact(read(), 5).status == Status::Disconnected);
  CHECK(s->releases == 1 && s->closes == 1 && s->shutdowns == 1 && s->transfers == 0);
}
}  // namespace
int main() {
  const std::pair<const char *, void(*)()> tests[] = {
    {"inert_construction_and_permission", inert_construction_and_permission},
    {"selection_and_cleanup", selection_and_cleanup}, {"reversed_selection", reversed_selection},
    {"reject_bootstub_and_dfu", reject_bootstub_and_dfu}, {"reject_duplicate_and_replacement", reject_duplicate_and_replacement},
    {"malformed_descriptors", malformed_descriptors}, {"active_alternate_needs_explicit_external_verification", active_alternate_needs_explicit_external_verification},
    {"open_failures_cleanup", open_failures_cleanup}, {"bulk_short_read_and_write", bulk_short_read_and_write},
    {"partial_timeout_preserves_data", partial_timeout_preserves_data},
    {"control_read_write_and_unknown_count", control_read_write_and_unknown_count},
    {"error_classes_and_no_replay", error_classes_and_no_replay}, {"invalid_requests_never_touch_api", invalid_requests_never_touch_api},
    {"impossible_actual_poisoned", impossible_actual_poisoned},
    {"stream_fragments_at_usb_packet_boundary", stream_fragments_at_usb_packet_boundary},
    {"two_session_parsers_do_not_mix_and_peer_survives_disconnect", two_session_parsers_do_not_mix_and_peer_survives_disconnect},
    {"cancelled_read_does_not_retry", cancelled_read_does_not_retry},
    {"separate_handles_progress_during_blocked_peer", separate_handles_progress_during_blocked_peer},
    {"close_does_not_reuse_session", close_does_not_reuse_session},
  };
  for (const auto &[name, test] : tests) { test(); std::cout << "PASS " << name << '\n'; }
  std::cout << "{\"suite\":\"usb_backend\",\"groups\":" << std::size(tests) << ",\"failed\":0,\"device_io\":false}\n";
}
