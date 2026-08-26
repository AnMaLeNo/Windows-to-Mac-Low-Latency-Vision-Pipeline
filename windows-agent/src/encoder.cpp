#include "encoder.h"

#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb_image_write.h"

#include <DirectXPackedVector.h>

#include <cmath>
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

// Maps every possible half-float bit pattern straight to an 8-bit sRGB value. A half is
// 16 bits, so the whole domain is only 65536 entries - which turns the per-pixel work
// (unpack half, normalize, clamp, apply the sRGB transfer curve, quantize) into a single
// table lookup. Doing that math per channel per pixel would mean ~270k pow() calls a
// frame; the table costs 64KB once.
std::vector<uint8_t> g_halfToSrgb8;

std::vector<uint8_t> rgbScratch;

}  // namespace

void InitHdrEncoding(float sdrWhiteScale) {
    if (sdrWhiteScale <= 0.0f) sdrWhiteScale = 1.0f;

    g_halfToSrgb8.resize(65536);
    for (int i = 0; i < 65536; ++i) {
        float v = DirectX::PackedVector::XMConvertHalfToFloat(static_cast<DirectX::PackedVector::HALF>(i));

        // Bring SDR white back to 1.0. Windows composites SDR content into the scRGB
        // buffer at the user's "SDR content brightness", not at scRGB 1.0.
        v /= sdrWhiteScale;

        // The comparison order also rejects NaN, whose every comparison is false.
        if (!(v > 0.0f)) v = 0.0f;
        if (v > 1.0f) v = 1.0f;

        // scRGB is linear; JPEG expects sRGB-encoded values.
        const float s = v <= 0.0031308f ? v * 12.92f : 1.055f * std::pow(v, 1.0f / 2.4f) - 0.055f;
        g_halfToSrgb8[i] = static_cast<uint8_t>(s * 255.0f + 0.5f);
    }
}

size_t EncodeFrameToJpeg(const uint8_t* srcData, uint32_t rowPitch, DXGI_FORMAT format, int width,
                          int height, int quality, uint8_t* dst, size_t dstCapacity) {
    // Reused across calls so we don't reallocate every frame - width/height are fixed for
    // the lifetime of the process.
    const size_t needed = static_cast<size_t>(width) * static_cast<size_t>(height) * 3;
    if (rgbScratch.size() < needed) rgbScratch.resize(needed);

    if (format == DXGI_FORMAT_R16G16B16A16_FLOAT) {
        if (g_halfToSrgb8.empty()) InitHdrEncoding(1.0f);  // defensive: caller should have done this
        for (int y = 0; y < height; ++y) {
            const auto* srcRow = reinterpret_cast<const uint16_t*>(srcData + static_cast<size_t>(y) * rowPitch);
            uint8_t* dstRow = rgbScratch.data() + static_cast<size_t>(y) * width * 3;
            for (int x = 0; x < width; ++x) {
                dstRow[x * 3 + 0] = g_halfToSrgb8[srcRow[x * 4 + 0]];  // R
                dstRow[x * 3 + 1] = g_halfToSrgb8[srcRow[x * 4 + 1]];  // G
                dstRow[x * 3 + 2] = g_halfToSrgb8[srcRow[x * 4 + 2]];  // B
            }
        }
    } else {
        // D3D11's mapped row pitch can include alignment padding beyond width*4 bytes, and
        // the JPEG encoder wants tightly-packed RGB (not BGRA) - this fixes both at once.
        for (int y = 0; y < height; ++y) {
            const uint8_t* srcRow = srcData + static_cast<size_t>(y) * rowPitch;
            uint8_t* dstRow = rgbScratch.data() + static_cast<size_t>(y) * width * 3;
            for (int x = 0; x < width; ++x) {
                dstRow[x * 3 + 0] = srcRow[x * 4 + 2];  // R
                dstRow[x * 3 + 1] = srcRow[x * 4 + 1];  // G
                dstRow[x * 3 + 2] = srcRow[x * 4 + 0];  // B
            }
        }
    }

    WriteCtx ctx{dst, dstCapacity, 0, false};
    stbi_write_jpg_to_func(WriteCallback, &ctx, width, height, 3, rgbScratch.data(), quality);
    return ctx.overflowed ? 0 : ctx.written;
}
