#pragma once
#include <d3d11.h>
#include <dxgi1_2.h>
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

private:
    void CreateDuplication();

    int m_roiX, m_roiY, m_roiWidth, m_roiHeight;

    Microsoft::WRL::ComPtr<IDXGIAdapter1> m_adapter;
    Microsoft::WRL::ComPtr<IDXGIOutput1> m_output1;
    Microsoft::WRL::ComPtr<ID3D11Device> m_device;
    Microsoft::WRL::ComPtr<ID3D11DeviceContext> m_context;
    Microsoft::WRL::ComPtr<IDXGIOutputDuplication> m_duplication;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> m_stagingTex;
};
