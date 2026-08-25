#include "capture.h"

#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>

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

    CreateDuplication();

    D3D11_TEXTURE2D_DESC stagingDesc = {};
    stagingDesc.Width = roiWidth;
    stagingDesc.Height = roiHeight;
    stagingDesc.MipLevels = 1;
    stagingDesc.ArraySize = 1;
    stagingDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    stagingDesc.SampleDesc.Count = 1;
    stagingDesc.Usage = D3D11_USAGE_STAGING;
    stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ThrowIfFailed(m_device->CreateTexture2D(&stagingDesc, nullptr, &m_stagingTex), "CreateTexture2D (staging)");
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
