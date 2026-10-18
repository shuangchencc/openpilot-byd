#pragma once
#include <array>
#include <functional>
#include <optional>
#include "pandad_loop.h"
namespace openpilot::pandad::compatibility {
enum class IngressTopic { DeviceState, SelfdriveState, CabinCameraState, Sendcan };
// The production-shaped boundary is independent of the transport. Receive is
// strictly nonblocking and returns an owned copy. Real msgq bindings below are
// constructed from already-open endpoints; none creates/connects a socket.
using ReceiveEvent = std::function<std::optional<MessageBytes>()>;
using PublishEvent = std::function<bool(OutputTopic,const MessageBytes &)>;
class RealtimeSource final : public EventSource {
public:
  explicit RealtimeSource(std::array<ReceiveEvent,4> channels) : channels_(std::move(channels)) {}
  std::optional<InputEnvelope> next(InputEpoch before_fetch) override;
  // CarParams has no timestamp/epoch in the schema: a new owner-held submission
  // is mandatory after every connection or control epoch. Never reload/cache it.
  bool submit_params(MessageBytes bytes, InputEpoch acquired);
  std::uint64_t discarded() const { return discarded_; }
private:
  std::array<ReceiveEvent,4> channels_;
  std::optional<InputEpoch> fenced_;
  std::optional<InputEnvelope> params_;
  std::size_t cursor_ = 0;
  std::uint64_t discarded_ = 0;
};
class RealtimeSink final : public EventSink {
public:
  explicit RealtimeSink(PublishEvent publish) : publish_(std::move(publish)) {}
  bool publish(OutputTopic topic,const MessageBytes &bytes) override;
private:
  PublishEvent publish_;
};
}  // namespace openpilot::pandad::compatibility
