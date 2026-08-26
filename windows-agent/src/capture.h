#pragma once
#include <d3d11.h>
#include <dxgi1_2.h>
#include <dxgi1_6.h>  // IDXGIOutput6 / DXGI_OUTPUT_DESC1, for HDR color-space detection
#include <wrl/client.h>
#include <cstdint>

// Wraps DXGI Desktop Duplication to repeatedly capture just a fixed ROI rectangle,
// cropped GPU-side, from the primary-like output (the one at virtual-desktop origin
// (0,0)) on the adapter that actually owns it.
class DesktopCapture {
public:
    DesktopCapture(int roiX, int roiY, int roiWidth, int roiHeight);
    ~DesktopCapture() = default;

    DesktopCapture(const DesktopCapture&) = delete;
    DesktopCapture& operator=(const DesktopCapture&) = delete;

    // Waits up to timeoutMs for a new frame. Returns true and sets *outData/*outRowPitch
    // when a real (non-cursor-only) frame was captured and GPU-cropped to the ROI; the
    // mapped memory stays valid until UnmapFrame() is called. Returns false on timeout, a
    // cursor-only update, or after recovering from a transient ACCESS_LOST - callers should
    // just loop back to AcquireFrame() in all false cases.
    bool AcquireFrame(uint32_t timeoutMs, const uint8_t** outData, uint32_t* outRowPitch);
    void UnmapFrame();

    // Pixel format of the mapped data. B8G8R8A8_UNORM normally; R16G16B16A16_FLOAT
    // (linear scRGB) when the display has HDR / Advanced Color enabled.
    DXGI_FORMAT Format() const { return m_format; }

    // scRGB value that corresponds to SDR white, i.e. the "SDR content brightness"
    // setting expressed as a multiple of scRGB 1.0 (= 80 nits). 1.0 when not in HDR.
    // The FP16 path divides by this to recover normal SDR appearance.
    float SdrWhiteScale() const { return m_sdrWhiteScale; }

private:
    void CreateDuplication();
    void CreateStagingTexture(DXGI_FORMAT format);

    int m_roiX, m_roiY, m_roiWidth, m_roiHeight;
    DXGI_FORMAT m_format = DXGI_FORMAT_B8G8R8A8_UNORM;
    float m_sdrWhiteScale = 1.0f;

    Microsoft::WRL::ComPtr<IDXGIAdapter1> m_adapter;
    Microsoft::WRL::ComPtr<IDXGIOutput1> m_output1;
    Microsoft::WRL::ComPtr<ID3D11Device> m_device;
    Microsoft::WRL::ComPtr<ID3D11DeviceContext> m_context;
    Microsoft::WRL::ComPtr<IDXGIOutputDuplication> m_duplication;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> m_stagingTex;
};
