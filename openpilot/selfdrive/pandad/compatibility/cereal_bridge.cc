#include "cereal_bridge.h"

#include <algorithm>
#include <cstring>
#include <limits>
#include <stdexcept>

#include <capnp/message.h>
#include <capnp/serialize.h>
#include "openpilot/cereal/gen/cpp/log.capnp.h"

namespace openpilot::pandad::compatibility {
namespace {
std::uint16_t u16(const MessageBytes &b, std::size_t n) { return b[n] | (std::uint16_t(b[n+1]) << 8); }
std::uint32_t u32(const MessageBytes &b, std::size_t n) { return u16(b,n) | (std::uint32_t(u16(b,n+2)) << 16); }
MessageBytes flatten(capnp::MallocMessageBuilder &builder) {
  const auto words = capnp::messageToFlatArray(builder); const auto bytes = words.asBytes();
  return {bytes.begin(), bytes.end()};
}
kj::Array<capnp::word> aligned(const MessageBytes &bytes) {
  if (bytes.empty() || bytes.size() > 65536 || bytes.size() % sizeof(capnp::word)) throw std::invalid_argument("invalid message extent");
  auto words = kj::heapArray<capnp::word>(bytes.size() / sizeof(capnp::word));
  std::memcpy(words.begin(), bytes.data(), bytes.size()); return words;
}
capnp::ReaderOptions options() { capnp::ReaderOptions o; o.traversalLimitInWords = 8192; o.nestingLimit = 16; return o; }
std::uint64_t timestamp(std::uint64_t ns) {
  // Ceiling prevents a future sub-microsecond timestamp becoming current by
  // truncation. Limits are checked before any additions/multiplications.
  return ns / 1000 + (ns % 1000 != 0);
}
std::uint64_t ns_time(std::uint64_t us) {
  if (us > UINT64_MAX / 1000) throw std::invalid_argument("timestamp overflow");
  return us * 1000;
}
cereal::PandaState::PandaType panda_type(const SessionBinding &binding) {
  return binding.expected.board_type == "DOS" ? cereal::PandaState::PandaType::DOS : cereal::PandaState::PandaType::RED_PANDA;
}
void fill_health(cereal::PandaState::Builder ps, const MessageBytes &h, const HealthFieldAvailability &available) {
  ps.setUptime(u32(h,0)); ps.setVoltage(available.voltage ? u16(h,4) : 0); ps.setCurrent(available.current ? u16(h,6) : 0);
  ps.setSafetyTxBlocked(u32(h,8)); ps.setSafetyRxInvalid(u32(h,12));
  ps.setTxBufferOverflow(u32(h,16)); ps.setRxBufferOverflow(u32(h,20));
  const auto bits = u32(h,24); unsigned count = 0;
  for (unsigned bit = 0; bit < 32; ++bit) count += (bits >> bit) & 1;
  auto faults = ps.initFaults(count); unsigned i = 0;
  for (unsigned bit = 0; bit < 32; ++bit) if ((bits >> bit) & 1) faults.set(i++, static_cast<cereal::PandaState::FaultType>(bit));
  const auto flags = u16(h,28);
  ps.setIgnitionLine(flags & 1); ps.setIgnitionCan(flags & 2); ps.setControlsAllowed(flags & 4);
  ps.setPowerSaveEnabled(flags & 8); ps.setHeartbeatLost(flags & 16); ps.setSafetyRxChecksInvalid(flags & 32);
  ps.setNmiReset(flags & 128); ps.setHardfaultReset(flags & 256);
  ps.setHarnessStatus(static_cast<cereal::PandaState::HarnessStatus>(h[30]));
  ps.setSafetyModel(static_cast<cereal::CarParams::SafetyModel>(h[31])); ps.setSafetyParam(u16(h,32));
  ps.setFaultStatus(static_cast<cereal::PandaState::FaultStatus>(h[34])); ps.setAlternativeExperience(u16(h,35));
  ps.setInterruptLoad(h[37] / 255.0f); ps.setFanPower(h[38]); ps.setSpiErrorCount(available.spi_error_count ? u16(h,39) : 0);
  ps.setSbu1Voltage(u16(h,41)/1000.0f); ps.setSbu2Voltage(u16(h,43)/1000.0f); ps.setSoundOutputLevel(available.sound_output ? u16(h,45) : 0);
}
void fill_can_health(cereal::PandaState::PandaCanState::Builder cs, const MessageBytes &h) {
  cs.setBusOff(h[0]); cs.setBusOffCnt(u32(h,1)); cs.setErrorWarning(h[5]); cs.setErrorPassive(h[6]);
  cs.setLastError(static_cast<cereal::PandaState::PandaCanState::LecErrorCode>(h[7]));
  cs.setLastStoredError(static_cast<cereal::PandaState::PandaCanState::LecErrorCode>(h[8]));
  cs.setLastDataError(static_cast<cereal::PandaState::PandaCanState::LecErrorCode>(h[9]));
  cs.setLastDataStoredError(static_cast<cereal::PandaState::PandaCanState::LecErrorCode>(h[10]));
  cs.setReceiveErrorCnt(h[11]); cs.setTransmitErrorCnt(h[12]); cs.setTotalErrorCnt(u32(h,13));
  cs.setTotalTxLostCnt(u32(h,17)); cs.setTotalRxLostCnt(u32(h,21)); cs.setTotalTxCnt(u32(h,25));
  cs.setTotalRxCnt(u32(h,29)); cs.setTotalFwdCnt(u32(h,33)); cs.setCanSpeed(u16(h,41)); cs.setCanDataSpeed(u16(h,43));
  cs.setCanfdEnabled(h[45]); cs.setBrsEnabled(h[46]); cs.setCanfdNonIso(h[47]);
  cs.setIrq0CallRate(u32(h,48)); cs.setIrq1CallRate(u32(h,52)); cs.setIrq2CallRate(u32(h,56)); cs.setCanCoreResetCnt(u32(h,60));
}
}
bool ingest_cereal_event(PandadRuntime &runtime, const MessageBytes &bytes, InputEpoch epoch) {
  // Reject old caller-held input before malformed bytes can trip a new session.
  const auto current = runtime.capture();
  if (epoch.generation != current.generation || epoch.control_epoch != current.control_epoch) return false;
  try {
    auto words = aligned(bytes); capnp::FlatArrayMessageReader reader(words, options());
    const auto event = reader.getRoot<cereal::Event>(); const auto time = timestamp(event.getLogMonoTime());
    if (event.getLogMonoTime() == 0) { runtime.invalid_input("MISSING_EVENT_TIMESTAMP"); return false; }
    switch (event.which()) {
      case cereal::Event::SELFDRIVE_STATE:
        return runtime.selfdrive(event.getValid(), event.getSelfdriveState().getEnabled(), time, epoch);
      case cereal::Event::DEVICE_STATE: {
        const auto ds = event.getDeviceState();
        return runtime.device_input({event.getValid(), ds.getStarted(),
          ds.getThermalStatus() == cereal::DeviceState::ThermalStatus::OK, ds.getFanSpeedPercentDesired(), time}, epoch);
      }
      case cereal::Event::CABIN_CAMERA_STATE: {
        const auto lines = event.getCabinCameraState().getIntegLines();
        const std::uint32_t percent = lines <= 400 ? 0 : lines >= 1000 ? 100 : (lines - 400) / 6;
        return runtime.camera_input({event.getValid(), percent, time}, epoch);
      }
      case cereal::Event::SENDCAN: {
        TxBatch batch; batch.valid = event.getValid(); batch.source_timestamp_us = time;
        const auto frames = event.getSendcan();
        if (frames.size() > kMaxTxBatchFrames) return false;
        for (const auto f : frames) {
          const auto data = f.getDat();
          if (data.size() > 64) return false;
          // The frozen schema has no FD flag: >8 bytes requires FD; classic
          // length<=8 remains classic, never infer FD support from hardware type.
          batch.frames.push_back({f.getAddress(), f.getSrc(), f.getAddress() >= 0x800,
            data.size() > 8, {data.begin(), data.end()}});
        }
        return runtime.sendcan(std::move(batch), epoch);
      }
      default: return false;
    }
  } catch (const kj::Exception &) { runtime.invalid_input("MALFORMED_CEREAL_EVENT"); }
    catch (const std::exception &) { runtime.invalid_input("MALFORMED_CEREAL_EVENT"); }
  return false;
}
bool configure_cereal_params(PandadRuntime &runtime, const MessageBytes &bytes, InputEpoch epoch) {
  const auto current = runtime.capture();
  if (epoch.generation != current.generation || epoch.control_epoch != current.control_epoch) return false;
  try {
    auto words = aligned(bytes); capnp::FlatArrayMessageReader reader(words, options());
    const auto cp = reader.getRoot<cereal::CarParams>(); const auto configs = cp.getSafetyConfigs();
    if (configs.size() != 2) { runtime.invalid_input("SAFETY_CONFIG_COUNT"); return false; }
    if (cp.getAlternativeExperience() < 0) { runtime.invalid_input("NEGATIVE_ALTERNATIVE_EXPERIENCE"); return false; }
    std::vector<SlotSafetyConfig> slots;
    for (unsigned slot = 0; slot < 2; ++slot) slots.push_back({slot,
      static_cast<std::uint32_t>(configs[slot].getSafetyModel()), configs[slot].getSafetyParam(), static_cast<std::uint32_t>(cp.getAlternativeExperience())});
    return runtime.configure(slots, epoch);
  } catch (const kj::Exception &) { runtime.invalid_input("MALFORMED_CAR_PARAMS"); }
    catch (const std::exception &) { runtime.invalid_input("MALFORMED_CAR_PARAMS"); }
  return false;
}
MessageBytes serialize_panda_states(PandadRuntime &runtime, std::uint64_t publish_us) {
  const auto state = runtime.snapshot(); const auto profile = runtime.profile();
  capnp::MallocMessageBuilder builder; auto event = builder.initRoot<cereal::Event>();
  auto source_us = publish_us;
  if (state.panda_states_valid) for (const auto &slot : state.slots) {
    source_us = std::min(source_us, slot.health_timestamp_us);
    for (const auto time : slot.can_health_timestamp_us) source_us = std::min(source_us, time);
  }
  event.setLogMonoTime(ns_time(source_us)); event.setValid(state.panda_states_valid);
  auto list = event.initPandaStates(2);
  for (unsigned slot = 0; slot < 2; ++slot) {
    auto ps = list[slot];
    if (!state.slots[slot].health_valid || !state.slots[slot].connected || !state.slots[slot].capabilities_valid) {
      ps.setPandaType(cereal::PandaState::PandaType::UNKNOWN);
      ps.setHeartbeatLost(true); ps.setControlsAllowed(false); continue;
    }
    ps.setPandaType(panda_type(profile.slots[slot])); fill_health(ps, state.slots[slot].health, state.slots[slot].field_available);
    const std::array builders{ps.initCanState0(), ps.initCanState1(), ps.initCanState2()};
    for (unsigned bus = 0; bus < 3; ++bus) if (state.slots[slot].can_health_valid[bus]) fill_can_health(builders[bus], state.slots[slot].can_health[bus]);
  }
  return flatten(builder);
}
MessageBytes serialize_peripheral_state(PandadRuntime &runtime, std::uint64_t publish_us) {
  const auto state = runtime.peripheral_snapshot(); const auto profile = runtime.profile();
  capnp::MallocMessageBuilder builder; auto event = builder.initRoot<cereal::Event>();
  // The explicit profile plus a valid capability handshake distinguishes F4
  // placeholders from measurements in message_policy. Required available fields
  // must be fresh. Never refresh the oldest observation timestamp.
  const bool valid = state.valid && state.fan_slot >= 0 && state.fan_rpm && state.voltage &&
    (!state.field_available.current || state.current);
  const auto source_us = valid ? std::min(state.timestamp_us, state.power_timestamp_us) : publish_us;
  event.setLogMonoTime(ns_time(source_us)); event.setValid(valid);
  auto ps = event.initPeripheralState();
  if (valid) {
    const auto slot = static_cast<unsigned>(state.fan_slot);
    ps.setPandaType(panda_type(profile.slots[slot])); ps.setFanSpeedRpm(*state.fan_rpm);
    ps.setVoltage(*state.voltage); ps.setCurrent(state.current.value_or(0));
  } else ps.setPandaType(cereal::PandaState::PandaType::UNKNOWN);
  return flatten(builder);
}
MessageBytes serialize_can(PandadRuntime &runtime, std::uint64_t publish_us) {
  auto frames = runtime.receive_frames(); const auto state = runtime.snapshot();
  capnp::MallocMessageBuilder builder; auto event = builder.initRoot<cereal::Event>();
  auto source_time = publish_us;
  for (const auto &frame : frames) source_time = std::min(source_time, frame.receive_timestamp_us);
  event.setLogMonoTime(ns_time(source_time)); event.setValid(state.panda_states_valid);
  auto list = event.initCan(frames.size());
  for (unsigned i = 0; i < frames.size(); ++i) {
    list[i].setAddress(frames[i].frame.address); list[i].setSrc(frames[i].source);
    list[i].setDat(kj::arrayPtr(frames[i].frame.payload.data(), frames[i].frame.payload.size()));
  }
  return flatten(builder);
}
}  // namespace openpilot::pandad::compatibility
