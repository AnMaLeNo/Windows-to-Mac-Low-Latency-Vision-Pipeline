#pragma once
#include <winsock2.h>

#include <cstddef>
#include <cstdint>
#include <string>

// One UDP socket, "connected" to a fixed target so per-frame sends are a plain send().
// Caller is responsible for WSAStartup/WSACleanup around this object's lifetime.
class UdpSender {
public:
    UdpSender(const std::string& targetIp, uint16_t port);
    ~UdpSender();

    UdpSender(const UdpSender&) = delete;
    UdpSender& operator=(const UdpSender&) = delete;

    void Send(const uint8_t* data, size_t size);

private:
    SOCKET m_socket;
};
