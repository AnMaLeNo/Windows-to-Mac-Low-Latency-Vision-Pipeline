#pragma once
#include <dxgiformat.h>

#include <cstddef>
#include <cstdint>

// Must be called once before encoding R16G16B16A16_FLOAT frames. Builds the half-float
// to sRGB lookup table for the given SDR white scale (see DesktopCapture::SdrWhiteScale).
void InitHdrEncoding(float sdrWhiteScale);

// Converts one captured frame to RGB and JPEG-encodes it, writing the encoded bytes
// directly into dst[0, dstCapacity). Handles both formats Desktop Duplication produces:
// B8G8R8A8_UNORM (plain 8-bit) and R16G16B16A16_FLOAT (linear scRGB, HDR displays).
// Returns bytes written, or 0 if the encoded JPEG would not have fit in dstCapacity.
// Not thread-safe (uses an internal scratch buffer) - fine for a single capture loop.
size_t EncodeFrameToJpeg(const uint8_t* srcData, uint32_t rowPitch, DXGI_FORMAT format, int width,
                          int height, int quality, uint8_t* dst, size_t dstCapacity);
