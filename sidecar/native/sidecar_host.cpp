// sidecar_host: low-latency capture + H.264 encode of one monitor, framed on stdout.
//
//   sidecar_host.exe (--gdi \\.\DISPLAYn | --primary) [--out WxH] [--fps 60] [--bitrate 30000000] [--gop 600]
//                    [--idle-ms 500] [--speed 50] [--sw] [--max-seconds N] [--verbose]
//
// Pipeline: Windows.Graphics.Capture (monitor; the cursor is composited by WGC, no yellow border)
//   -> D3D11 video processor BGRA->NV12 (+ optional scaling, GPU)
//   -> H.264: async hardware MFT (AMD AMF / Intel QSV / NVENC MFT; low-latency CBR, no B-frames)
//      or, with --sw, the Microsoft software H.264 MFT (CPU, NV12 read back from the GPU).
//
// Safety design (an earlier version froze the machine):
//  * every D3D11 immediate-context call and every MFT call happens on ONE worker thread;
//    the WGC callback only hands over the frame object, the MFT event thread only queues event types;
//  * single instance (named mutex) so two encoder sessions never overlap;
//  * orderly teardown (END_OF_STREAM, IMFShutdown, release) with a hard 3 s exit watchdog;
//  * optional --max-seconds self-termination for time-boxed tests.
//
// stdout packets: [u32 BE payload length][u8 flags][u64 BE pts_us] payload
//   flags bit0 = keyframe, 0x80 = JSON info message instead of video. pts_us = QPC capture time in microseconds
//   (comparable to Python's time.perf_counter_ns() // 1000).
// stdin commands (one per line): K = keyframe now, B <bps> = bitrate, Q = quit. EOF = quit.
// Exit codes: 0 quit, 2 init error, 3 monitor size changed (restart me), 4 monitor not found, 5 already running.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11_4.h>
#include <dxgi1_6.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mftransform.h>
#include <mferror.h>
#include <codecapi.h>
#include <strmif.h>
#include <wmcodecdsp.h>
#include <fcntl.h>
#include <io.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <winrt/base.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <winrt/Windows.Security.Authorization.AppCapabilityAccess.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mfuuid.lib")
#pragma comment(lib, "strmiids.lib")
#pragma comment(lib, "wmcodecdspuuid.lib")
#pragma comment(lib, "windowsapp.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "winmm.lib")

using winrt::com_ptr;
namespace wgc = winrt::Windows::Graphics::Capture;
namespace wd3d = winrt::Windows::Graphics::DirectX::Direct3D11;

static void logf(const char* fmt, ...) {
    va_list a; va_start(a, fmt);
    vfprintf(stderr, fmt, a); fputc('\n', stderr); fflush(stderr);
    va_end(a);
}
#define CHECK(x) do { HRESULT _hr = (x); if (FAILED(_hr)) { logf("%s failed: 0x%08lx (line %d)", #x, (unsigned long)_hr, __LINE__); return false; } } while (0)

static LARGE_INTEGER qpf;
static int64_t nowUs() { LARGE_INTEGER q; QueryPerformanceCounter(&q); return q.QuadPart * 1000000 / qpf.QuadPart; }

// ------------------------------------------------------------------ output
static void writePacket(uint8_t flags, uint64_t ptsUs, const void* data, uint32_t len) {
    uint8_t h[13];
    h[0] = len >> 24; h[1] = len >> 16; h[2] = len >> 8; h[3] = (uint8_t)len;
    h[4] = flags;
    for (int i = 0; i < 8; i++) h[5 + i] = (uint8_t)(ptsUs >> (56 - 8 * i));
    fwrite(h, 1, 13, stdout);
    fwrite(data, 1, len, stdout);
    fflush(stdout);
}
static void writeInfo(const std::string& json) { writePacket(0x80, 0, json.data(), (uint32_t)json.size()); }

// ------------------------------------------------------------------ options / state
struct Opts {
    std::wstring gdi; bool primary = false;
    int outW = 0, outH = 0, fps = 60, bitrate = 30000000, gop = 600, idleMs = 500, speed = 50, maxSeconds = 0;
    int rc = 1;  // 0 = CBR, 1 = low-delay VBR
    bool sw = false, verbose = false, ignoreStdin = false;
} opt;

static com_ptr<ID3D11Device> dev;
static com_ptr<ID3D11DeviceContext> ctx;
static com_ptr<ID3D11VideoDevice> vdev;
static com_ptr<ID3D11VideoContext> vctx;
static com_ptr<ID3D11VideoProcessor> vproc;
static com_ptr<ID3D11VideoProcessorEnumerator> venum;
static com_ptr<ID3D11Texture2D> latest;           // last captured BGRA frame (capture size)
static com_ptr<ID3D11VideoProcessorInputView> latestView;
static const int RING = 6;
static com_ptr<ID3D11Texture2D> nv12[RING];
static com_ptr<ID3D11VideoProcessorOutputView> nv12View[RING];
static com_ptr<ID3D11Texture2D> staging;          // --sw: CPU readback
static int CW = 0, CH = 0;   // capture size
static int W = 0, H = 0;     // encode size

static com_ptr<IMFTransform> mft;
static com_ptr<IMFMediaEventGenerator> mftEvents;
static com_ptr<ICodecAPI> codec;
static com_ptr<IMFDXGIDeviceManager> devMgr;
static DWORD outStreamFlags = 0, outCb = 0;
static std::string encoderName;
static bool asyncMft = false;

// Shared between threads (guarded by mu): only plain data and WinRT frame objects, never D3D/MFT calls.
static std::mutex mu;
static std::condition_variable cv;
static wgc::Direct3D11CaptureFrame pendingFrame{ nullptr };
static std::deque<MediaEventType> events;
static std::atomic<bool> quit{ false }, keyReq{ true };
static std::atomic<int> exitCode{ 0 };
static std::atomic<int> newBitrate{ 0 };
static std::atomic<int> stCap{ 0 }, stIn{ 0 }, stOut{ 0 };
static std::atomic<int64_t> stEncUs{ 0 };

static void requestQuit(int code = -1) {
    if (code >= 0) exitCode = code;
    quit = true;
    cv.notify_all();
}

// ------------------------------------------------------------------ helpers
static HMONITOR findMonitor() {
    if (opt.primary) return MonitorFromPoint(POINT{ 0, 0 }, MONITOR_DEFAULTTOPRIMARY);
    struct C { std::wstring name; HMONITOR found = nullptr; } c{ opt.gdi };
    EnumDisplayMonitors(nullptr, nullptr, [](HMONITOR m, HDC, LPRECT, LPARAM p) -> BOOL {
        auto* c = (C*)p;
        MONITORINFOEXW mi{}; mi.cbSize = sizeof(mi);
        GetMonitorInfoW(m, &mi);
        if (_wcsicmp(mi.szDevice, c->name.c_str()) == 0) { c->found = m; return FALSE; }
        return TRUE;
    }, (LPARAM)&c);
    return c.found;
}

static std::string narrow(const wchar_t* w) {
    std::string s; for (; *w; w++) s += (*w < 128 && *w != '"' && *w != '\\') ? (char)*w : '?';
    return s;
}

// ------------------------------------------------------------------ video processor
static bool initVideoProcessor() {
    CHECK(dev->QueryInterface(IID_PPV_ARGS(vdev.put())));
    CHECK(ctx->QueryInterface(IID_PPV_ARGS(vctx.put())));
    D3D11_VIDEO_PROCESSOR_CONTENT_DESC cd{};
    cd.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
    cd.InputWidth = CW; cd.InputHeight = CH; cd.OutputWidth = W; cd.OutputHeight = H;
    cd.InputFrameRate = { (UINT)opt.fps, 1 }; cd.OutputFrameRate = { (UINT)opt.fps, 1 };
    cd.Usage = D3D11_VIDEO_USAGE_OPTIMAL_SPEED;
    CHECK(vdev->CreateVideoProcessorEnumerator(&cd, venum.put()));
    CHECK(vdev->CreateVideoProcessor(venum.get(), 0, vproc.put()));
    D3D11_VIDEO_PROCESSOR_COLOR_SPACE in{}; in.RGB_Range = 0;  // full-range RGB
    D3D11_VIDEO_PROCESSOR_COLOR_SPACE out{}; out.YCbCr_Matrix = 1; out.Nominal_Range = D3D11_VIDEO_PROCESSOR_NOMINAL_RANGE_16_235;
    vctx->VideoProcessorSetStreamColorSpace(vproc.get(), 0, &in);
    vctx->VideoProcessorSetOutputColorSpace(vproc.get(), &out);
    vctx->VideoProcessorSetStreamAutoProcessingMode(vproc.get(), 0, FALSE);
    RECT src{ 0, 0, CW, CH }, dst{ 0, 0, W, H };
    vctx->VideoProcessorSetStreamSourceRect(vproc.get(), 0, TRUE, &src);
    vctx->VideoProcessorSetStreamDestRect(vproc.get(), 0, TRUE, &dst);
    vctx->VideoProcessorSetOutputTargetRect(vproc.get(), TRUE, &dst);

    D3D11_TEXTURE2D_DESC td{};
    td.Width = CW; td.Height = CH; td.MipLevels = 1; td.ArraySize = 1; td.SampleDesc.Count = 1;
    td.Format = DXGI_FORMAT_B8G8R8A8_UNORM; td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    CHECK(dev->CreateTexture2D(&td, nullptr, latest.put()));
    D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC ivd{}; ivd.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
    CHECK(vdev->CreateVideoProcessorInputView(latest.get(), venum.get(), &ivd, latestView.put()));
    td.Width = W; td.Height = H;
    td.Format = DXGI_FORMAT_NV12; td.BindFlags = D3D11_BIND_RENDER_TARGET | (opt.sw ? 0 : D3D11_BIND_VIDEO_ENCODER);
    D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC ovd{}; ovd.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
    for (int i = 0; i < RING; i++) {
        HRESULT hr = dev->CreateTexture2D(&td, nullptr, nv12[i].put());
        if (FAILED(hr)) { td.BindFlags = D3D11_BIND_RENDER_TARGET; CHECK(dev->CreateTexture2D(&td, nullptr, nv12[i].put())); }
        CHECK(vdev->CreateVideoProcessorOutputView(nv12[i].get(), venum.get(), &ovd, nv12View[i].put()));
    }
    if (opt.sw) {
        D3D11_TEXTURE2D_DESC sd = td;
        sd.BindFlags = 0; sd.Usage = D3D11_USAGE_STAGING; sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        CHECK(dev->CreateTexture2D(&sd, nullptr, staging.put()));
    }
    return true;
}

// ------------------------------------------------------------------ encoder
static bool setCodec(const GUID& g, UINT32 v) {
    if (!codec) return false;
    VARIANT var; VariantInit(&var); var.vt = VT_UI4; var.ulVal = v;
    HRESULT hr = codec->SetValue(&g, &var);
    if (FAILED(hr) && opt.verbose) logf("codecapi set failed 0x%08lx", (unsigned long)hr);
    return SUCCEEDED(hr);
}
static void setCodecBool(const GUID& g, bool v) {
    if (!codec) return;
    VARIANT var; VariantInit(&var); var.vt = VT_BOOL; var.boolVal = v ? VARIANT_TRUE : VARIANT_FALSE;
    codec->SetValue(&g, &var);
}

static bool createEncoder() {
    if (opt.sw) {
        CHECK(CoCreateInstance(CLSID_MSH264EncoderMFT, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(mft.put())));
        encoderName = "Microsoft H.264 (software)";
        return true;
    }
    MFT_REGISTER_TYPE_INFO in{ MFMediaType_Video, MFVideoFormat_NV12 }, out{ MFMediaType_Video, MFVideoFormat_H264 };
    IMFActivate** acts = nullptr; UINT32 n = 0;
    CHECK(MFTEnumEx(MFT_CATEGORY_VIDEO_ENCODER, MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER, &in, &out, &acts, &n));
    if (n == 0) { logf("no hardware H.264 encoder MFT"); return false; }
    com_ptr<IDXGIDevice> dxgiDev; dev->QueryInterface(IID_PPV_ARGS(dxgiDev.put()));
    com_ptr<IDXGIAdapter> ad; dxgiDev->GetAdapter(ad.put());
    DXGI_ADAPTER_DESC adesc{}; ad->GetDesc(&adesc);
    UINT pick = 0;
    for (UINT i = 0; i < n; i++) {
        UINT64 luid = 0;
        if (SUCCEEDED(acts[i]->GetUINT64(MFT_ENUM_ADAPTER_LUID, &luid)) &&
            luid == (((UINT64)(UINT32)adesc.AdapterLuid.HighPart << 32) | adesc.AdapterLuid.LowPart)) { pick = i; break; }
    }
    wchar_t name[256] = L"?"; UINT32 nl = 0;
    acts[pick]->GetString(MFT_FRIENDLY_NAME_Attribute, name, 256, &nl);
    encoderName = narrow(name);
    HRESULT hr = acts[pick]->ActivateObject(IID_PPV_ARGS(mft.put()));
    for (UINT i = 0; i < n; i++) acts[i]->Release();
    CoTaskMemFree(acts);
    CHECK(hr);
    return true;
}

static bool initEncoder() {
    if (!createEncoder()) return false;
    com_ptr<IMFAttributes> attrs;
    if (SUCCEEDED(mft->GetAttributes(attrs.put())) && attrs) {
        UINT32 async = 0;
        attrs->GetUINT32(MF_TRANSFORM_ASYNC, &async);
        asyncMft = async != 0;
        if (asyncMft) CHECK(attrs->SetUINT32(MF_TRANSFORM_ASYNC_UNLOCK, TRUE));
        attrs->SetUINT32(MF_LOW_LATENCY, TRUE);
    }
    if (!opt.sw && !asyncMft) { logf("hardware encoder is not async (unsupported)"); return false; }
    if (asyncMft) CHECK(mft->QueryInterface(IID_PPV_ARGS(mftEvents.put())));
    mft->QueryInterface(IID_PPV_ARGS(codec.put()));

    if (!opt.sw) {
        UINT token = 0;
        CHECK(MFCreateDXGIDeviceManager(&token, devMgr.put()));
        CHECK(devMgr->ResetDevice(dev.get(), token));
        CHECK(mft->ProcessMessage(MFT_MESSAGE_SET_D3D_MANAGER, (ULONG_PTR)devMgr.get()));
    }

    // Low-delay VBR: no filler data on static desktops, bounded peaks for motion. Falls back to peak-constrained VBR / CBR.
    if (opt.rc == 0 || (!setCodec(CODECAPI_AVEncCommonRateControlMode, eAVEncCommonRateControlMode_LowDelayVBR) &&
                        !setCodec(CODECAPI_AVEncCommonRateControlMode, eAVEncCommonRateControlMode_PeakConstrainedVBR)))
        setCodec(CODECAPI_AVEncCommonRateControlMode, eAVEncCommonRateControlMode_CBR);
    setCodec(CODECAPI_AVEncCommonMeanBitRate, opt.bitrate);
    setCodec(CODECAPI_AVEncCommonMaxBitRate, opt.bitrate * 3 / 2);
    setCodecBool(CODECAPI_AVLowLatencyMode, true);
    setCodec(CODECAPI_AVEncMPVDefaultBPictureCount, 0);
    setCodec(CODECAPI_AVEncMPVGOPSize, opt.gop);
    setCodec(CODECAPI_AVEncCommonQualityVsSpeed, opt.speed);

    com_ptr<IMFMediaType> ot;
    CHECK(MFCreateMediaType(ot.put()));
    ot->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    ot->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
    ot->SetUINT32(MF_MT_AVG_BITRATE, opt.bitrate);
    MFSetAttributeSize(ot.get(), MF_MT_FRAME_SIZE, W, H);
    MFSetAttributeRatio(ot.get(), MF_MT_FRAME_RATE, opt.fps, 1);
    MFSetAttributeRatio(ot.get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    ot->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    ot->SetUINT32(MF_MT_MPEG2_PROFILE, opt.sw ? eAVEncH264VProfile_Base : eAVEncH264VProfile_High);
    ot->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
    ot->SetUINT32(MF_MT_VIDEO_PRIMARIES, MFVideoPrimaries_BT709);
    ot->SetUINT32(MF_MT_TRANSFER_FUNCTION, MFVideoTransFunc_709);
    ot->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235);
    CHECK(mft->SetOutputType(0, ot.get(), 0));

    com_ptr<IMFMediaType> it;
    CHECK(MFCreateMediaType(it.put()));
    it->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    it->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    MFSetAttributeSize(it.get(), MF_MT_FRAME_SIZE, W, H);
    MFSetAttributeRatio(it.get(), MF_MT_FRAME_RATE, opt.fps, 1);
    MFSetAttributeRatio(it.get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    it->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    it->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
    it->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235);
    CHECK(mft->SetInputType(0, it.get(), 0));

    MFT_OUTPUT_STREAM_INFO si{};
    CHECK(mft->GetOutputStreamInfo(0, &si));
    outStreamFlags = si.dwFlags; outCb = si.cbSize;

    CHECK(mft->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0));
    CHECK(mft->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0));
    return true;
}

// Pulls one output sample. Returns S_OK, MF_E_TRANSFORM_NEED_MORE_INPUT or an error.
static HRESULT pullOutput() {
    MFT_OUTPUT_DATA_BUFFER ob{};
    com_ptr<IMFSample> own;
    if (!(outStreamFlags & (MFT_OUTPUT_STREAM_PROVIDES_SAMPLES | MFT_OUTPUT_STREAM_CAN_PROVIDE_SAMPLES))) {
        MFCreateSample(own.put());
        com_ptr<IMFMediaBuffer> b;
        MFCreateMemoryBuffer(std::max<DWORD>(outCb, (DWORD)(W * H * 2)), b.put());
        own->AddBuffer(b.get());
        ob.pSample = own.get();
    }
    DWORD status = 0;
    HRESULT hr = mft->ProcessOutput(0, 1, &ob, &status);
    if (ob.pEvents) ob.pEvents->Release();
    IMFSample* s = ob.pSample;
    if (hr == MF_E_TRANSFORM_STREAM_CHANGE) {
        com_ptr<IMFMediaType> t;
        if (SUCCEEDED(mft->GetOutputAvailableType(0, 0, t.put()))) mft->SetOutputType(0, t.get(), 0);
        if (s && !own) s->Release();
        return S_OK;
    }
    if (FAILED(hr)) { if (s && !own) s->Release(); return hr; }
    if (!s) return S_OK;
    com_ptr<IMFMediaBuffer> buf;
    if (SUCCEEDED(s->ConvertToContiguousBuffer(buf.put()))) {
        BYTE* p = nullptr; DWORD len = 0;
        if (SUCCEEDED(buf->Lock(&p, nullptr, &len))) {
            LONGLONG t = 0; s->GetSampleTime(&t);
            UINT32 clean = 0; s->GetUINT32(MFSampleExtension_CleanPoint, &clean);
            writePacket(clean ? 1 : 0, (uint64_t)(t / 10), p, len);
            stOut++;
            buf->Unlock();
        }
    }
    if (!own) s->Release();
    return S_OK;
}

// MFT event pump: only queues event types for the worker (no MFT/D3D calls here besides GetEvent).
static void eventThread() {
    while (true) {
        com_ptr<IMFMediaEvent> ev;
        HRESULT hr = mftEvents->GetEvent(0, ev.put());
        if (FAILED(hr)) break;   // MF_E_SHUTDOWN on teardown
        MediaEventType t = 0; ev->GetType(&t);
        { std::lock_guard<std::mutex> lk(mu); events.push_back(t); }
        cv.notify_all();
    }
}

// Converts `latest` into an NV12 sample and feeds the encoder. Worker thread only.
static bool encodeLatest(int64_t ts, int& slot) {
    if (int b = newBitrate.exchange(0)) setCodec(CODECAPI_AVEncCommonMeanBitRate, b);
    if (keyReq.exchange(false)) setCodec(CODECAPI_AVEncVideoForceKeyFrame, 1);
    D3D11_VIDEO_PROCESSOR_STREAM st{}; st.Enable = TRUE; st.pInputSurface = latestView.get();
    HRESULT hr = vctx->VideoProcessorBlt(vproc.get(), nv12View[slot].get(), 0, 1, &st);
    if (FAILED(hr)) { logf("VideoProcessorBlt 0x%08lx", (unsigned long)hr); return false; }
    com_ptr<IMFMediaBuffer> mb;
    if (opt.sw) {
        ctx->CopyResource(staging.get(), nv12[slot].get());
        D3D11_MAPPED_SUBRESOURCE m{};
        if (FAILED(ctx->Map(staging.get(), 0, D3D11_MAP_READ, 0, &m))) return false;
        DWORD size = W * H * 3 / 2;
        MFCreateMemoryBuffer(size, mb.put());
        BYTE* dst = nullptr;
        mb->Lock(&dst, nullptr, nullptr);
        const BYTE* src = (const BYTE*)m.pData;
        for (int y = 0; y < H; y++) memcpy(dst + y * W, src + y * m.RowPitch, W);
        const BYTE* uv = src + (size_t)m.RowPitch * H;
        for (int y = 0; y < H / 2; y++) memcpy(dst + W * H + y * W, uv + y * m.RowPitch, W);
        mb->Unlock();
        mb->SetCurrentLength(size);
        ctx->Unmap(staging.get(), 0);
    } else {
        ctx->Flush();   // submit the conversion now; the encoder engine waits on it
        if (FAILED(MFCreateDXGISurfaceBuffer(__uuidof(ID3D11Texture2D), nv12[slot].get(), 0, FALSE, mb.put()))) return false;
    }
    com_ptr<IMFSample> s;
    MFCreateSample(s.put());
    s->AddBuffer(mb.get());
    s->SetSampleTime(ts * 10);
    s->SetSampleDuration(10000000 / opt.fps);
    int64_t t0 = nowUs();
    hr = mft->ProcessInput(0, s.get(), 0);
    if (FAILED(hr)) { logf("ProcessInput 0x%08lx", (unsigned long)hr); return false; }
    stIn++;
    slot = (slot + 1) % RING;
    if (!asyncMft) {
        while (pullOutput() == S_OK) {}
        stEncUs += nowUs() - t0;
    }
    return true;
}

static void worker() {
    int slot = 0, needInput = asyncMft ? 0 : 1;
    bool haveFrame = false, fresh = false;
    int64_t latestTs = 0;
    auto lastEncode = std::chrono::steady_clock::now();
    auto deadline = opt.maxSeconds > 0 ? std::chrono::steady_clock::now() + std::chrono::seconds(opt.maxSeconds)
                                       : std::chrono::steady_clock::time_point::max();
    while (!quit) {
        wgc::Direct3D11CaptureFrame frame{ nullptr };
        std::deque<MediaEventType> evs;
        {
            std::unique_lock<std::mutex> lk(mu);
            cv.wait_for(lk, std::chrono::milliseconds(50), [] { return quit || pendingFrame || !events.empty(); });
            frame = std::move(pendingFrame);
            pendingFrame = nullptr;
            evs.swap(events);
        }
        if (quit) break;
        if (std::chrono::steady_clock::now() > deadline) { logf("max-seconds reached"); break; }
        for (auto t : evs) {
            if (t == METransformNeedInput) needInput++;
            else if (t == METransformHaveOutput) pullOutput();
        }
        if (frame) {
            auto cs = frame.ContentSize();
            if (cs.Width < CW || cs.Height < CH || cs.Width > CW + 1 || cs.Height > CH + 1) {
                logf("monitor size changed to %dx%d", cs.Width, cs.Height);
                exitCode = 3; frame.Close(); break;
            }
            auto access = frame.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
            com_ptr<ID3D11Texture2D> tex;
            access->GetInterface(IID_PPV_ARGS(tex.put()));
            D3D11_BOX box{ 0, 0, 0, (UINT)CW, (UINT)CH, 1 };
            ctx->CopySubresourceRegion(latest.get(), 0, 0, 0, 0, tex.get(), 0, &box);
            latestTs = frame.SystemRelativeTime().count() / 10;
            tex = nullptr;
            frame.Close();   // return the buffer to the pool right away
            haveFrame = fresh = true;
            stCap++;
        }
        if (!haveFrame || needInput <= 0) continue;
        bool idle = std::chrono::steady_clock::now() - lastEncode >= std::chrono::milliseconds(opt.idleMs);
        if (!fresh && !keyReq && !idle) continue;
        int64_t ts = fresh ? latestTs : nowUs();
        fresh = false;
        if (encodeLatest(ts, slot)) {
            if (asyncMft) needInput--;
            lastEncode = std::chrono::steady_clock::now();
        }
    }
}

static void stdinThread() {
    char line[256];
    while (fgets(line, sizeof(line), stdin)) {
        if (line[0] == 'Q') break;
        if (line[0] == 'K') { keyReq = true; cv.notify_all(); }
        if (line[0] == 'B') newBitrate = atoi(line + 1);
    }
    requestQuit();
}

int wmain(int argc, wchar_t** argv) {
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    QueryPerformanceFrequency(&qpf);
    _setmode(_fileno(stdout), _O_BINARY);
    for (int i = 1; i < argc; i++) {
        std::wstring a = argv[i];
        auto next = [&](int& v) { if (i + 1 < argc) v = _wtoi(argv[++i]); };
        if (a == L"--gdi" && i + 1 < argc) opt.gdi = argv[++i];
        else if (a == L"--primary") opt.primary = true;
        else if (a == L"--out" && i + 1 < argc) { swscanf_s(argv[++i], L"%dx%d", &opt.outW, &opt.outH); }
        else if (a == L"--fps") next(opt.fps);
        else if (a == L"--bitrate") next(opt.bitrate);
        else if (a == L"--gop") next(opt.gop);
        else if (a == L"--idle-ms") next(opt.idleMs);
        else if (a == L"--speed") next(opt.speed);
        else if (a == L"--max-seconds") next(opt.maxSeconds);
        else if (a == L"--sw") opt.sw = true;
        else if (a == L"--verbose") opt.verbose = true;
        else if (a == L"--ignore-stdin") opt.ignoreStdin = true;
        else if (a == L"--cbr") opt.rc = 0;
    }
    HANDLE single = CreateMutexW(nullptr, TRUE, L"Local\\PhoneSyncSidecarHost");
    if (GetLastError() == ERROR_ALREADY_EXISTS) { logf("another sidecar_host is running"); return 5; }

    winrt::init_apartment(winrt::apartment_type::multi_threaded);
    HMONITOR mon = findMonitor();
    if (!mon) { logf("monitor not found"); return 4; }

    UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT;
    D3D_FEATURE_LEVEL fl[] = { D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0 };
    if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags, fl, 2, D3D11_SDK_VERSION, dev.put(), nullptr, ctx.put()))) {
        logf("D3D11CreateDevice failed"); return 2;
    }
    // The encoder MFT uses the device from its own threads.
    { com_ptr<ID3D10Multithread> mt; ctx->QueryInterface(IID_PPV_ARGS(mt.put())); if (mt) mt->SetMultithreadProtected(TRUE); }

    auto interop = winrt::get_activation_factory<wgc::GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
    wgc::GraphicsCaptureItem item{ nullptr };
    if (FAILED(interop->CreateForMonitor(mon, winrt::guid_of<wgc::GraphicsCaptureItem>(), winrt::put_abi(item)))) {
        logf("CreateForMonitor failed"); return 2;
    }
    auto size = item.Size();
    CW = size.Width & ~1; CH = size.Height & ~1;
    W = opt.outW > 0 ? opt.outW & ~1 : CW;
    H = opt.outH > 0 ? opt.outH & ~1 : CH;
    if (FAILED(MFStartup(MF_VERSION, MFSTARTUP_FULL))) { logf("MFStartup failed"); return 2; }
    if (!initVideoProcessor() || !initEncoder()) return 2;

    com_ptr<IDXGIDevice> dxgi; dev->QueryInterface(IID_PPV_ARGS(dxgi.put()));
    winrt::com_ptr<::IInspectable> insp;
    if (FAILED(CreateDirect3D11DeviceFromDXGIDevice(dxgi.get(), insp.put()))) { logf("CreateDirect3D11DeviceFromDXGIDevice"); return 2; }
    auto rtDev = insp.as<wd3d::IDirect3DDevice>();
    auto pool = wgc::Direct3D11CaptureFramePool::CreateFreeThreaded(rtDev,
        winrt::Windows::Graphics::DirectX::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, size);
    auto session = pool.CreateCaptureSession(item);
    bool borderless = false;
    try {
        auto st = wgc::GraphicsCaptureAccess::RequestAccessAsync(wgc::GraphicsCaptureAccessKind::Borderless).get();
        session.IsBorderRequired(false);
        borderless = st == winrt::Windows::Security::Authorization::AppCapabilityAccess::AppCapabilityAccessStatus::Allowed;
    } catch (...) {}
    try { session.IsCursorCaptureEnabled(true); } catch (...) {}
    try { session.MinUpdateInterval(winrt::Windows::Foundation::TimeSpan{ 10000000 / (opt.fps * 2) }); } catch (...) {}

    // Capture callback: hand over the newest frame object only (dropping an older unconsumed one).
    pool.FrameArrived([](wgc::Direct3D11CaptureFramePool const& p, auto&&) {
        auto frame = p.TryGetNextFrame();
        if (!frame) return;
        wgc::Direct3D11CaptureFrame old{ nullptr };
        {
            std::lock_guard<std::mutex> lk(mu);
            old = std::move(pendingFrame);
            pendingFrame = frame;
        }
        if (old) old.Close();
        cv.notify_all();
    });

    std::thread evt;
    if (asyncMft) evt = std::thread(eventThread);
    if (!opt.ignoreStdin) std::thread(stdinThread).detach();
    session.StartCapture();
    char info[512];
    snprintf(info, sizeof(info), "{\"w\":%d,\"h\":%d,\"capture_w\":%d,\"capture_h\":%d,\"fps\":%d,\"bitrate\":%d,\"encoder\":\"%s\",\"borderless\":%s}",
        W, H, CW, CH, opt.fps, opt.bitrate, encoderName.c_str(), borderless ? "true" : "false");
    writeInfo(info);
    logf("capturing %dx%d -> %dx%d with %s", CW, CH, W, H, encoderName.c_str());
    if (opt.verbose) std::thread([] {
        while (!quit) {
            Sleep(1000);
            int o = stOut.exchange(0);
            logf("stats cap=%d in=%d out=%d enc_ms=%.1f", stCap.exchange(0), stIn.exchange(0), o, o ? stEncUs.exchange(0) / 1000.0 / o : 0.0);
        }
    }).detach();

    worker();

    // Orderly teardown, guarded by a hard watchdog.
    quit = true;
    std::thread([] { Sleep(3000); logf("teardown watchdog"); fflush(stdout); ExitProcess(exitCode.load()); }).detach();
    try { session.Close(); pool.Close(); } catch (...) {}
    {
        std::lock_guard<std::mutex> lk(mu);
        if (pendingFrame) { pendingFrame.Close(); pendingFrame = nullptr; }
    }
    mft->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
    mft->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
    if (asyncMft) {
        com_ptr<IMFShutdown> sd;
        if (SUCCEEDED(mft->QueryInterface(IID_PPV_ARGS(sd.put())))) sd->Shutdown();
        if (evt.joinable()) evt.join();
    }
    if (devMgr) mft->ProcessMessage(MFT_MESSAGE_SET_D3D_MANAGER, 0);
    codec = nullptr; mftEvents = nullptr; mft = nullptr; devMgr = nullptr;
    ctx->ClearState(); ctx->Flush();
    MFShutdown();
    fflush(stdout);
    ReleaseMutex(single);
    return exitCode.load();
}
