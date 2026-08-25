# windows-agent

C++ capture/send agent. No third-party package manager needed — everything is either part of
the Windows SDK (already installed with Visual Studio) or vendored directly in `third_party/`.

## Build

From this folder, using the CMake bundled with Visual Studio 2022 (not on PATH by default):

```powershell
$cmake = "C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
& $cmake --preset x64-release
& $cmake --build build --config Release
```

The resulting executable is at `build\Release\capture_agent.exe`. (Opening this folder
directly in Visual Studio also works — it auto-detects `CMakePresets.json`.)

## Run

- `capture_agent.exe --dump 10` — capture 10 real (non-cursor-only) frames and write them as
  `out\capture_0000.jpg` … to disk. No networking involved; use this first to confirm the ROI
  position/size and JPEG colors look right.
- `capture_agent.exe` — run the real capture → encode → UDP send loop, targeting the IP/port
  configured as constants at the top of `src/main.cpp`. Ctrl+C to stop.

ROI position/size, JPEG quality, and the target IP/port are plain constants near the top of
`src/main.cpp` — edit and rebuild rather than adding a config file, for now.
