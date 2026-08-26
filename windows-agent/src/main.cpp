#include "capture.h"
#include "encoder.h"
#include "network.h"
#include "protocol.h"

#include <windows.h>
#include <winsock2.h>

#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

// --- v1 configuration: plain constants, edit + rebuild rather than adding a config file ---
constexpr int kRoiX = 1130;  // centered on a 2560x1440 primary screen
constexpr int kRoiY = 570;
constexpr int kRoiWidth = 300;
constexpr int kRoiHeight = 300;
// Keep this above 90: stb_image_write switches off 4:2:0 chroma subsampling only above
// that threshold (stb_image_write.h:1479), and halved color resolution visibly smears
// sharp UI content. Measured on real screen content with --bench: q85 = 7.4KB / 0.70ms
// but subsampled, q95 = 13.7KB / 1.12ms at full color. Raw RGB would be 270KB, which
// exceeds the 65507-byte UDP datagram limit outright.
constexpr int kJpegQuality = 95;
constexpr const char* kMacIp = "192.168.1.127";  // milestone 3 smoke test over local Wi-Fi, pending the dedicated Ethernet link
constexpr size_t kMaxJpegSize = 65507 - sizeof(PacketHeader);  // keeps the whole UDP payload under the safe limit

volatile std::sig_atomic_t g_stopRequested = 0;
void OnSigInt(int) { g_stopRequested = 1; }

struct WinsockGuard {
    WinsockGuard() {
        WSADATA wsaData;
        if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
            throw std::runtime_error("WSAStartup failed");
        }
    }
    ~WinsockGuard() { WSACleanup(); }
};

// Milestone 1: capture N real frames and write them to disk, no networking at all. Verifies
// ROI position/size and JPEG colors before any wire-protocol code is involved.
int RunDumpMode(DesktopCapture& capture, int frameCount) {
    std::vector<uint8_t> jpegBuf(kMaxJpegSize);
    CreateDirectoryA("out", nullptr);  // no-op if it already exists

    int written = 0;
    while (written < frameCount && !g_stopRequested) {
        const uint8_t* mappedData = nullptr;
        uint32_t rowPitch = 0;
        if (!capture.AcquireFrame(500, &mappedData, &rowPitch)) {
            continue;  // timeout, cursor-only update, or ACCESS_LOST recovery - just retry
        }

        size_t jpegSize = EncodeBgraToJpeg(mappedData, rowPitch, kRoiWidth, kRoiHeight, kJpegQuality,
                                            jpegBuf.data(), jpegBuf.size());
        capture.UnmapFrame();

        if (jpegSize == 0) {
            std::cerr << "warning: encoded JPEG did not fit in the buffer, skipping frame\n";
            continue;
        }

        char path[MAX_PATH];
        std::snprintf(path, sizeof(path), "out\\capture_%04d.jpg", written);
        if (FILE* f = std::fopen(path, "wb")) {
            std::fwrite(jpegBuf.data(), 1, jpegSize, f);
            std::fclose(f);
        }
        std::cout << "wrote " << path << " (" << jpegSize << " bytes)\n";
        ++written;
    }
    return 0;
}

// Captures one real frame and encodes it across a sweep of JPEG qualities, reporting size
// and encode cost for each. Answers "what is compression actually buying, and what does
// better quality cost?" against real screen content rather than guesswork. Note the jump
// at quality 91: stb_image_write switches off 4:2:0 chroma subsampling above 90
// (stb_image_write.h:1479), which is what fixes color smearing on sharp UI content.
int RunBenchMode(DesktopCapture& capture) {
    const uint8_t* mappedData = nullptr;
    uint32_t rowPitch = 0;
    while (!capture.AcquireFrame(500, &mappedData, &rowPitch)) {
        if (g_stopRequested) return 0;
    }

    std::vector<uint8_t> buf(kMaxJpegSize);
    const size_t rawSize = static_cast<size_t>(kRoiWidth) * kRoiHeight * 3;
    std::cout << "\nRaw RGB " << kRoiWidth << "x" << kRoiHeight << " = " << rawSize
              << " bytes (UDP datagram limit is 65507 - does not fit)\n\n"
              << "quality   bytes   vs raw   encode   chroma\n";

    for (int q : {70, 80, 85, 90, 91, 95, 100}) {
        // Encode a few times and keep the best - one-shot timings at this scale are
        // dominated by scheduler noise.
        size_t size = 0;
        double bestMs = 1e9;
        for (int i = 0; i < 5; ++i) {
            const auto t0 = std::chrono::steady_clock::now();
            size = EncodeBgraToJpeg(mappedData, rowPitch, kRoiWidth, kRoiHeight, q, buf.data(), buf.size());
            const double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
            if (ms < bestMs) bestMs = ms;
        }
        std::printf("%7d %7zu %7.1fx %7.2fms   %s\n", q, size, static_cast<double>(rawSize) / size, bestMs,
                    q <= 90 ? "4:2:0 (halved)" : "4:4:4 (full)");
    }

    capture.UnmapFrame();
    std::cout << "\n";
    return 0;
}

// Milestones 2/3: the real capture -> encode -> UDP send loop.
int RunNetworkedLoop(DesktopCapture& capture) {
    WinsockGuard winsockGuard;
    UdpSender sender(kMacIp, kUdpPort);

    std::vector<uint8_t> packetBuffer(sizeof(PacketHeader) + kMaxJpegSize);
    auto* header = reinterpret_cast<PacketHeader*>(packetBuffer.data());
    uint8_t* jpegDst = packetBuffer.data() + sizeof(PacketHeader);

    uint32_t sequence = 0;

    std::cout << "Sending to " << kMacIp << ":" << kUdpPort << ". Ctrl+C to stop.\n";

    while (!g_stopRequested) {
        const uint8_t* mappedData = nullptr;
        uint32_t rowPitch = 0;
        if (!capture.AcquireFrame(500, &mappedData, &rowPitch)) {
            continue;
        }

        // Both clocks are read here, immediately after the frame arrives - NOT before
        // AcquireFrame, which blocks until the desktop changes. Timing from before the
        // block would fold idle waiting time into the measurement and make encode look
        // far more expensive than it is.
        const auto frameAcquired = std::chrono::steady_clock::now();
        const auto captureWallclockUs = std::chrono::duration_cast<std::chrono::microseconds>(
                                             std::chrono::system_clock::now().time_since_epoch())
                                             .count();

        size_t jpegSize = EncodeBgraToJpeg(mappedData, rowPitch, kRoiWidth, kRoiHeight, kJpegQuality, jpegDst,
                                            kMaxJpegSize);
        capture.UnmapFrame();

        if (jpegSize == 0) {
            std::cerr << "warning: encoded JPEG did not fit in the buffer, dropping frame\n";
            continue;
        }

        const auto sendStart = std::chrono::steady_clock::now();
        header->sequence = sequence++;
        header->capture_wallclock_us = static_cast<uint64_t>(captureWallclockUs);
        header->capture_to_send_us = static_cast<uint32_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(sendStart - frameAcquired).count());
        header->width = static_cast<uint16_t>(kRoiWidth);
        header->height = static_cast<uint16_t>(kRoiHeight);
        header->jpeg_size = static_cast<uint32_t>(jpegSize);

        sender.Send(packetBuffer.data(), sizeof(PacketHeader) + jpegSize);
    }

    std::cout << "Stopped after " << sequence << " frames sent.\n";
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, OnSigInt);

    const std::string mode = argc >= 2 ? argv[1] : "";
    int dumpFrameCount = 0;
    if (argc >= 3 && mode == "--dump") {
        dumpFrameCount = std::atoi(argv[2]);
    }

    try {
        std::cout << "ROI=(" << kRoiX << "," << kRoiY << ") " << kRoiWidth << "x" << kRoiHeight << "\n";
        DesktopCapture capture(kRoiX, kRoiY, kRoiWidth, kRoiHeight);

        if (mode == "--bench") {
            return RunBenchMode(capture);
        }
        if (dumpFrameCount > 0) {
            std::cout << "Dump mode: capturing " << dumpFrameCount << " real frames to out\\capture_NNNN.jpg\n";
            return RunDumpMode(capture, dumpFrameCount);
        }
        return RunNetworkedLoop(capture);

    } catch (const std::exception& e) {
        std::cerr << "Fatal: " << e.what() << std::endl;
        return 1;
    }
}
