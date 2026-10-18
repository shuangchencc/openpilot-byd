#include "realtime_bridge.h"
#include <cstring>
#include <stdexcept>
#include <capnp/message.h>
#include <capnp/serialize.h>
#include "openpilot/cereal/gen/cpp/log.capnp.h"
namespace openpilot::pandad::compatibility {
namespace {
bool same(InputEpoch a,InputEpoch b) { return a.generation==b.generation && a.control_epoch==b.control_epoch; }
void inspect(const MessageBytes &bytes,const std::function<void(cereal::Event::Reader)> &fn) {
  if (bytes.empty() || bytes.size()>65536 || bytes.size()%8) throw std::invalid_argument("invalid message extent");
  auto words=kj::heapArray<capnp::word>(bytes.size()/8); std::memcpy(words.begin(),bytes.data(),bytes.size());
  capnp::ReaderOptions opts; opts.traversalLimitInWords=8192;opts.nestingLimit=16;
  capnp::FlatArrayMessageReader reader(words,opts); fn(reader.getRoot<cereal::Event>());
}
}
bool RealtimeSource::submit_params(MessageBytes bytes,InputEpoch acquired) {
  if (params_ || bytes.empty() || bytes.size()>65536 || bytes.size()%8) return false;
  params_=InputEnvelope{InputKind::CarParams,std::move(bytes),acquired}; return true;
}
std::optional<InputEnvelope> RealtimeSource::next(InputEpoch before_fetch) {
  if (!fenced_ || !same(*fenced_,before_fetch)) {
    // Bound draining to 16 per channel. A continuously producing/stuck queue
    // fails closed instead of blocking or accepting old messages with new epoch.
    for (auto &receive : channels_) {
      if (!receive) throw std::invalid_argument("missing ingress channel");
      bool empty=false;
      for (std::size_t n=0;n<=kLoopMaxInputs;++n) {
        if (!receive()) { empty=true; break; }
        ++discarded_;
      }
      if (!empty) throw std::runtime_error("ingress fence backlog");
    }
    if (params_ && !same(params_->epoch,before_fetch)) { params_.reset();++discarded_; }
    fenced_=before_fetch;
    return std::nullopt;
  }
  if (params_) { auto item=std::move(params_);params_.reset();return item; }
  for (unsigned count=0;count<channels_.size();++count) {
    const auto index=cursor_++%channels_.size(); auto bytes=channels_[index](); if (!bytes) continue;
    inspect(*bytes,[&](cereal::Event::Reader event) {
      const std::array kinds{cereal::Event::DEVICE_STATE,cereal::Event::SELFDRIVE_STATE,
                             cereal::Event::CABIN_CAMERA_STATE,cereal::Event::SENDCAN};
      if (event.which()!=kinds[index]) throw std::invalid_argument("topic/event mismatch");
      // Integration is an observation-only candidate. Keep old P6 simulation
      // tests available, but never let this runtime adapter activate output.
      if (event.isSendcan() || (event.isSelfdriveState() && event.getSelfdriveState().getEnabled()))
        throw std::invalid_argument("noOutput ingress rejected");
    });
    return InputEnvelope{InputKind::Event,std::move(*bytes),before_fetch};
  }
  return std::nullopt;
}
bool RealtimeSink::publish(OutputTopic topic,const MessageBytes &bytes) {
  if (!publish_) return false;
  inspect(bytes,[&](cereal::Event::Reader event) {
    const bool matches=(topic==OutputTopic::Can && event.isCan()) ||
      (topic==OutputTopic::PandaStates && event.isPandaStates()) ||
      (topic==OutputTopic::PeripheralState && event.isPeripheralState());
    if (!matches) throw std::invalid_argument("publication topic/event mismatch");
  });
  return publish_(topic,bytes);
}
}  // namespace openpilot::pandad::compatibility
