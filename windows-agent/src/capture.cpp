#include "capture.h"

#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {

std::string HrToString(HRESULT hr) {
    std::ostringstream oss;
    oss << "0x" << std::hex << std::setw(8) << std::setfill('0') << static_cast<unsigned long>(hr);
    return oss.str();
}

void ThrowIfFailed(HRESULT hr, const char* what) {
    if (FAILED(hr)) {
        throw std::runtime_error(std::string(what) + " failed, hr=" + HrToString(hr));
    }
}

// With HDR on, Windows composites SDR content into the scRGB buffer scaled up to the
// user's "SDR content brightness" setting rather than to scRGB 1.0 (= 80 nits). Without
// dividing that back out, ordinary white lands well above 1.0 and everything clips to a
// blown-out highlight. Returns the scale as a multiple of scRGB 1.0, or 1.0 if it can't
// be determined (also the correct value when HDR is off).
float QuerySdrWhiteScale(const WCHAR* gdiDeviceName) {
    UINT32 pathCount = 0, modeCount = 0;
    if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &pathCount, &modeCount) != ERROR_SUCCESS) {
        return 1.0f;
    }
    std::vector<DISPLAYCONFIG_PATH_INFO> paths(pathCount);
    std::vector<DISPLAYCONFIG_MODE_INFO> modes(modeCount);
    if (QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &pathCount, paths.data(), &modeCount, modes.data(),
                           nullptr) != ERROR_SUCCESS) {
        return 1.0f;
    }

    for (UINT32 i = 0; i < pathCount; ++i) {
        DISPLAYCONFIG_SOURCE_DEVICE_NAME source = {};
        source.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
        source.header.size = sizeof(source);
        source.header.adapterId = paths[i].sourceInfo.adapterId;
        source.header.id = paths[i].sourceInfo.id;
        if (DisplayConfigGetDeviceInfo(&source.header) != ERROR_SUCCESS) continue;
        if (wcscmp(source.viewGdiDeviceName, gdiDeviceName) != 0) continue;

        DISPLAYCONFIG_SDR_WHITE_LEVEL white = {};
        white.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL;
        white.header.size = sizeof(white);
        white.header.adapterId = paths[i].targetInfo.adapterId;
        white.header.id = paths[i].targetInfo.id;
        if (DisplayConfigGetDeviceInfo(&white.header) != ERROR_SUCCESS) return 1.0f;
        // SDRWhiteLevel is in thousandths of the scRGB 1.0 reference.
        return white.SDRWhiteLevel > 0 ? white.SDRWhiteLevel / 1000.0f : 1.0f;
    }
    return 1.0f;
}

}  // namespace

DesktopCapture::DesktopCapture(int roiX, int roiY, int roiWidth, int roiHeight)
    : m_roiX(roiX), m_roiY(roiY), m_roiWidth(roiWidth), m_roiHeight(roiHeight) {
    ComPtr<IDXGIFactory1> factory;
    ThrowIfFailed(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "CreateDXGIFactory1");

    // Find every desktop-attached output, preferring the one sitting at virtual-desktop
    // origin (0,0) - by Windows convention that's the primary monitor, and it's what a
    // user configuring ROI x/y=0,0 naturally expects. Fall back to the first attached
    // output found if none happens to sit exactly at the origin.
    ComPtr<IDXGIAdapter1> foundAdapter, fallbackAdapter;
    ComPtr<IDXGIOutput> foundOutput, fallbackOutput;
    DXGI_OUTPUT_DESC foundDesc{}, fallbackDesc{};

    for (UINT ai = 0;; ++ai) {
        ComPtr<IDXGIAdapter1> adapter;
        if (factory->EnumAdapters1(ai, &adapter) == DXGI_ERROR_NOT_FOUND) break;

        for (UINT oi = 0;; ++oi) {
            ComPtr<IDXGIOutput> output;
            if (adapter->EnumOutputs(oi, &output) == DXGI_ERROR_NOT_FOUND) break;

            DXGI_OUTPUT_DESC desc;
            if (FAILED(output->GetDesc(&desc)) || !desc.AttachedToDesktop) continue;

            if (!fallbackAdapter) {
                fallbackAdapter = adapter;
                fallbackOutput = output;
                fallbackDesc = desc;
            }
            if (desc.DesktopCoordinates.left == 0 && desc.DesktopCoordinates.top == 0) {
                foundAdapter = adapter;
                foundOutput = output;
                foundDesc = desc;
            }
        }
    }
    if (!foundAdapter) {
        foundAdapter = fallbackAdapter;
        foundOutput = fallbackOutput;
        foundDesc = fallbackDesc;
    }
    if (!foundAdapter || !foundOutput) {
        throw std::runtime_error("No desktop-attached output found for DXGI Desktop Duplication");
    }

    DXGI_ADAPTER_DESC1 adapterDesc;
    foundAdapter->GetDesc1(&adapterDesc);
    std::wcout << L"Using adapter: " << adapterDesc.Description << L", output: " << foundDesc.DeviceName
               << L"\n";

    const LONG outW = foundDesc.DesktopCoordinates.right - foundDesc.DesktopCoordinates.left;
    const LONG outH = foundDesc.DesktopCoordinates.bottom - foundDesc.DesktopCoordinates.top;
    if (roiX < 0 || roiY < 0 || roiX + roiWidth > outW || roiY + roiHeight > outH) {
        std::ostringstream oss;
        oss << "ROI (" << roiX << "," << roiY << " " << roiWidth << "x" << roiHeight
            << ") does not fit inside the captured output's resolution (" << outW << "x" << outH << ")";
        throw std::runtime_error(oss.str());
    }

    m_adapter = foundAdapter;

    // Device MUST be created on this exact adapter (not D3D_DRIVER_TYPE_HARDWARE with a
    // null adapter), or DuplicateOutput() below fails with DXGI_ERROR_UNSUPPORTED on
    // hybrid-graphics (integrated + discrete GPU) laptops when Windows hands the default
    // device to the wrong GPU.
    D3D_FEATURE_LEVEL featureLevels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0,
                                          D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0};
    D3D_FEATURE_LEVEL achievedLevel;
    ThrowIfFailed(D3D11CreateDevice(m_adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, featureLevels,
                                     ARRAYSIZE(featureLevels), D3D11_SDK_VERSION, &m_device, &achievedLevel,
                                     &m_context),
                  "D3D11CreateDevice");

    ThrowIfFailed(foundOutput.As(&m_output1), "QueryInterface IDXGIOutput1");

    // HDR being on wrecks the captured image, so say so loudly rather than letting it look
    // like a bug elsewhere. Measured on this setup with a known grayscale ramp: the 8-bit
    // surface Desktop Duplication hands back contains the content multiplied by the SDR
    // white level (3.0 here) and re-encoded to sRGB, so everything above displayed value
    // ~156 clips to pure white. Captured 17->35, 102->169, 154->252, 179->255, 230->255.
    // That clipping happens before we ever see the pixels, so no post-processing can undo
    // it - the only real fix is turning HDR off.
    ComPtr<IDXGIOutput6> output6;
    if (SUCCEEDED(foundOutput.As(&output6))) {
        DXGI_OUTPUT_DESC1 desc1;
        if (SUCCEEDED(output6->GetDesc1(&desc1))) {
            const bool hdr = desc1.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
            std::cout << "Output color space: " << (hdr ? "HDR10 (PQ)" : "SDR (sRGB)") << ", "
                      << desc1.BitsPerColor << " bits/channel, max luminance " << desc1.MaxLuminance
                      << " nits\n";
            if (hdr) {
                const float scale = QuerySdrWhiteScale(foundDesc.DeviceName);
                std::cout << "\n  *** WARNING: HDR is enabled on this display. ***\n"
                          << "  Captured highlights will clip to pure white (SDR white is at " << scale
                          << "x, so anything\n"
                          << "  brighter than ~" << static_cast<int>(255.0f / scale * 1.84f)
                          << "/255 saturates). This is baked into the captured pixels and cannot be\n"
                          << "  corrected afterwards. Turn HDR off in Settings > System > Display > HDR.\n\n";
            }
        }
    }

    CreateDuplication();

    m_sdrWhiteScale = QuerySdrWhiteScale(foundDesc.DeviceName);
    // The staging texture is created lazily on the first acquired frame, from that
    // texture's own format. DXGI_OUTDUPL_DESC.ModeDesc.Format is NOT reliable here: with
    // HDR on it reports R16G16B16A16_FLOAT while the surface actually handed over is
    // B8G8R8A8_UNORM. Trusting it makes the staging format mismatch, and
    // CopySubresourceRegion has no HRESULT to return - it just silently copies nothing
    // and every frame comes out black.

}

void DesktopCapture::CreateStagingTexture(DXGI_FORMAT format) {
    if (format != DXGI_FORMAT_B8G8R8A8_UNORM && format != DXGI_FORMAT_R16G16B16A16_FLOAT) {
        std::ostringstream oss;
        oss << "Desktop Duplication handed over unsupported format " << format << " (expected "
            << DXGI_FORMAT_B8G8R8A8_UNORM << " or " << DXGI_FORMAT_R16G16B16A16_FLOAT << ")";
        throw std::runtime_error(oss.str());
    }
    m_format = format;

    D3D11_TEXTURE2D_DESC stagingDesc = {};
    stagingDesc.Width = m_roiWidth;
    stagingDesc.Height = m_roiHeight;
    stagingDesc.MipLevels = 1;
    stagingDesc.ArraySize = 1;
    stagingDesc.Format = format;
    stagingDesc.SampleDesc.Count = 1;
    stagingDesc.Usage = D3D11_USAGE_STAGING;
    stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ThrowIfFailed(m_device->CreateTexture2D(&stagingDesc, nullptr, &m_stagingTex), "CreateTexture2D (staging)");

    std::cout << "Capture format: " << format
              << (format == DXGI_FORMAT_R16G16B16A16_FLOAT
                      ? " (scRGB half-float, tone mapping to sRGB)"
                      : " (8-bit BGRA)")
              << "\n";
}

void DesktopCapture::CreateDuplication() {
    m_duplication.Reset();
    ThrowIfFailed(m_output1->DuplicateOutput(m_device.Get(), &m_duplication), "DuplicateOutput");
}

bool DesktopCapture::AcquireFrame(uint32_t timeoutMs, const uint8_t** outData, uint32_t* outRowPitch) {
    DXGI_OUTDUPL_FRAME_INFO frameInfo;
    ComPtr<IDXGIResource> desktopResource;
    HRESULT hr = m_duplication->AcquireNextFrame(timeoutMs, &frameInfo, &desktopResource);

    if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
        return false;
    }
    if (hr == DXGI_ERROR_ACCESS_LOST) {
        // Desktop switch (lock screen / UAC prompt / resolution change / GPU reset).
        // Recreate the duplication interface on the same device and let the caller retry.
        CreateDuplication();
        return false;
    }
    ThrowIfFailed(hr, "AcquireNextFrame");

    if (frameInfo.LastPresentTime.QuadPart == 0) {
        // Cursor-only update (DXGI wakes AcquireNextFrame on mouse movement too) - the
        // actual desktop image didn't change, so there's nothing worth encoding/sending.
        m_duplication->ReleaseFrame();
        return false;
    }

    ComPtr<ID3D11Texture2D> frameTex;
    ThrowIfFailed(desktopResource.As(&frameTex), "QueryInterface ID3D11Texture2D");

    // The acquired texture is the only authority on format. Build the staging texture to
    // match it on first use, and rebuild if it ever changes underneath us (an HDR toggle
    // arrives as ACCESS_LOST followed by a differently-formatted surface).
    D3D11_TEXTURE2D_DESC frameDesc;
    frameTex->GetDesc(&frameDesc);
    if (!m_stagingTex || frameDesc.Format != m_format) {
        CreateStagingTexture(frameDesc.Format);
    }

    D3D11_BOX box;
    box.left = static_cast<UINT>(m_roiX);
    box.top = static_cast<UINT>(m_roiY);
    box.front = 0;
    box.right = static_cast<UINT>(m_roiX + m_roiWidth);
    box.bottom = static_cast<UINT>(m_roiY + m_roiHeight);
    box.back = 1;
    m_context->CopySubresourceRegion(m_stagingTex.Get(), 0, 0, 0, 0, frameTex.Get(), 0, &box);

    // We've already copied what we need onto the GPU side - release the duplication's lock
    // on the desktop image as early as possible rather than holding it through Map()/encode.
    m_duplication->ReleaseFrame();

    D3D11_MAPPED_SUBRESOURCE mapped;
    ThrowIfFailed(m_context->Map(m_stagingTex.Get(), 0, D3D11_MAP_READ, 0, &mapped), "Map staging texture");
    *outData = static_cast<const uint8_t*>(mapped.pData);
    *outRowPitch = mapped.RowPitch;
    return true;
}

void DesktopCapture::UnmapFrame() { m_context->Unmap(m_stagingTex.Get(), 0); }
