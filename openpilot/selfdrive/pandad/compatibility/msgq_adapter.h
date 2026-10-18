#pragma once
#include "realtime_bridge.h"
#include "msgq/ipc.h"
namespace openpilot::pandad::compatibility {
// Caller owns all endpoints through the complete owner-loop lifetime. Endpoints
// must use isolated names during tests. These functions never create/connect.
ReceiveEvent bind_msgq_receiver(SubSocket &socket);
PublishEvent bind_msgq_publishers(const std::array<PubSocket *,3> &sockets);
}  // namespace openpilot::pandad::compatibility
