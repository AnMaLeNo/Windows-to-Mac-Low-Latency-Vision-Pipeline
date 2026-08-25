#include "encoder.h"

#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb_image_write.h"

#include <cstring>
#include <vector>

namespace {

struct WriteCtx {
    uint8_t* dst;
    size_t capacity;
    size_t written;
    bool overflowed;
};

void WriteCallback(void* context, void* data, int size) {
    auto* ctx = static_cast<WriteCtx*>(context);
    if (ctx->overflowed) return;
    if (ctx->written + static_cast<size_t>(size) > ctx->capacity) {
        ctx->overflowed = true;
        return;
    }
    std::memcpy(ctx->dst + ctx->written, data, static_cast<size_t>(size));
    ctx->written += static_cast<size_t>(size);
}

}  // namespace

size_t EncodeBgraToJpeg(const uint8_t* bgraData, uint32_t rowPitch, int width, int height, int quality,
                         uint8_t* dst, size_t dstCapacity) {
    // Reused across calls so we don't reallocate every frame - safe since width/height are
    // fixed for the lifetime of this process.
    static std::vector<uint8_t> rgbScratch;
    const size_t needed = static_cast<size_t>(width) * static_cast<size_t>(height) * 3;
    if (rgbScratch.size() < needed) rgbScratch.resize(needed);

    // D3D11's mapped row pitch can include alignment padding beyond width*4 bytes, and the
    // JPEG encoder wants tightly-packed RGB (not BGRA) - this loop fixes both at once.
    for (int y = 0; y < height; ++y) {
        const uint8_t* srcRow = bgraData + static_cast<size_t>(y) * rowPitch;
        uint8_t* dstRow = rgbScratch.data() + static_cast<size_t>(y) * width * 3;
        for (int x = 0; x < width; ++x) {
            dstRow[x * 3 + 0] = srcRow[x * 4 + 2];  // R
            dstRow[x * 3 + 1] = srcRow[x * 4 + 1];  // G
            dstRow[x * 3 + 2] = srcRow[x * 4 + 0];  // B
        }
    }

    WriteCtx ctx{dst, dstCapacity, 0, false};
    stbi_write_jpg_to_func(WriteCallback, &ctx, width, height, 3, rgbScratch.data(), quality);
    return ctx.overflowed ? 0 : ctx.written;
}
