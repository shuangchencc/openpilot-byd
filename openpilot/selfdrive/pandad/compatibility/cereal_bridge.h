#pragma once
#include "pandad_runtime.h"
namespace openpilot::pandad::compatibility {
using MessageBytes = std::vector<std::uint8_t>;
// Uses the pinned, unchanged cereal schema. The event epoch is captured by the
// owner BEFORE reading/deserializing input; it is not encoded or authenticated
// by cereal v1. Outputs must be consumed with Event.valid and source age checks.
bool ingest_cereal_event(PandadRuntime &runtime, const MessageBytes &bytes, InputEpoch epoch);
bool configure_cereal_params(PandadRuntime &runtime, const MessageBytes &bytes, InputEpoch epoch);
MessageBytes serialize_panda_states(PandadRuntime &runtime, std::uint64_t publish_us);
MessageBytes serialize_peripheral_state(PandadRuntime &runtime, std::uint64_t publish_us);
MessageBytes serialize_can(PandadRuntime &runtime, std::uint64_t publish_us);
}  // namespace openpilot::pandad::compatibility
