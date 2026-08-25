cmake_minimum_required(VERSION 3.21)
project(WindowsCaptureAgent CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(capture_agent
    src/main.cpp
    src/capture.cpp
    src/encoder.cpp
    src/network.cpp
)

target_include_directories(capture_agent PRIVATE src third_party)
target_link_libraries(capture_agent PRIVATE d3d11.lib dxgi.lib Ws2_32.lib)
target_compile_definitions(capture_agent PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)
