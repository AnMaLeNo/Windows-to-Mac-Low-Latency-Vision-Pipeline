#pragma once
#include <cstddef>
#include <cstdint>

// Repacks a BGRA buffer (as produced by a mapped D3D11 staging texture, alpha ignored) into
// RGB and JPEG-encodes it, writing the encoded bytes directly into dst[0, dstCapacity).
// Returns the number of bytes written, or 0 if the encoded JPEG would not have fit in
// dstCapacity. Not thread-safe (uses an internal scratch buffer) - fine for this project's
// single-threaded capture loop.
size_t EncodeBgraToJpeg(const uint8_t* bgraData, uint32_t rowPitch, int width, int height, int quality,
                         uint8_t* dst, size_t dstCapacity);
