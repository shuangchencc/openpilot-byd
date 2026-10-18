#include "msgq_adapter.h"
#include <memory>
#include <stdexcept>
namespace openpilot::pandad::compatibility {
ReceiveEvent bind_msgq_receiver(SubSocket &socket) {
  return [&socket]() -> std::optional<MessageBytes> {
    std::unique_ptr<Message> message(socket.receive(true));
    if (!message) return std::nullopt;
    if (!message->getSize() || message->getSize()>65536 || !message->getData()) throw std::invalid_argument("msgq message extent");
    const auto *start=reinterpret_cast<const std::uint8_t *>(message->getData());
    return MessageBytes(start,start+message->getSize());
  };
}
PublishEvent bind_msgq_publishers(const std::array<PubSocket *,3> &sockets) {
  for (const auto *socket:sockets) if (!socket) throw std::invalid_argument("missing publication socket");
  return [sockets](OutputTopic topic,const MessageBytes &bytes) {
    const auto index=static_cast<std::size_t>(topic);
    if(index>=sockets.size() || bytes.empty() || bytes.size()>65536) return false;
    // Pinned msgq send returns bytes sent, negative on failure.
    return sockets[index]->send(reinterpret_cast<char *>(const_cast<std::uint8_t *>(bytes.data())),bytes.size()) == static_cast<int>(bytes.size());
  };
}
}  // namespace openpilot::pandad::compatibility
