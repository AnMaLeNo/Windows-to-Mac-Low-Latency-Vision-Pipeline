#pragma once
#include <cstdint>

#pragma pack(push, 1)
struct PacketHeader {
    uint32_t sequence;
    uint64_t capture_wallclock_us;  // Unix epoch microseconds (UTC) at the moment the frame was captured
    uint32_t capture_to_send_us;
    uint16_t width;
    uint16_t height;
    uint32_t jpeg_size;
};
#pragma pack(pop)

static_assert(sizeof(PacketHeader) == 24, "PacketHeader size drifted - check padding/pack(1)");

constexpr uint16_t kUdpPort = 50505;
