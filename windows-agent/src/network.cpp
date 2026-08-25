#include "network.h"

#include <ws2tcpip.h>

#include <stdexcept>

#pragma comment(lib, "Ws2_32.lib")

UdpSender::UdpSender(const std::string& targetIp, uint16_t port) {
    m_socket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (m_socket == INVALID_SOCKET) {
        throw std::runtime_error("socket() failed, WSAGetLastError=" + std::to_string(WSAGetLastError()));
    }

    sockaddr_in target{};
    target.sin_family = AF_INET;
    target.sin_port = htons(port);
    if (InetPtonA(AF_INET, targetIp.c_str(), &target.sin_addr) != 1) {
        closesocket(m_socket);
        throw std::runtime_error("Invalid target IP address: " + targetIp);
    }

    // connect() on a UDP socket doesn't create a real connection (UDP is connectionless) -
    // it just fixes the peer address so Send() below can use send() instead of sendto().
    if (connect(m_socket, reinterpret_cast<sockaddr*>(&target), sizeof(target)) == SOCKET_ERROR) {
        closesocket(m_socket);
        throw std::runtime_error("connect() failed, WSAGetLastError=" + std::to_string(WSAGetLastError()));
    }
}

UdpSender::~UdpSender() { closesocket(m_socket); }

void UdpSender::Send(const uint8_t* data, size_t size) {
    send(m_socket, reinterpret_cast<const char*>(data), static_cast<int>(size), 0);
}
