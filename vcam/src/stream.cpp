// MediaStream: single NV12 video stream fed from shared memory.
// Adapted from smourier/VCamSample MediaStream.cpp (MIT).
#include "source.h"
#include "shm.h"

static const UINT32 kFps = 30;
static const LONGLONG kFrameDuration = 10000000LL / kFps; // 100ns

static LONGLONG Now100ns()
{
    static LARGE_INTEGER freq = {};
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    LARGE_INTEGER c;
    QueryPerformanceCounter(&c);
    return (LONGLONG)((double)c.QuadPart * 10000000.0 / (double)freq.QuadPart);
}

static volatile LONG g_activeStreams = 0;

static void UpdateConsumerActive(bool started, UINT32 w, UINT32 h)
{
    LONG n = started ? InterlockedIncrement(&g_activeStreams) : InterlockedDecrement(&g_activeStreams);
    if (n < 0) { InterlockedExchange(&g_activeStreams, 0); n = 0; }
    SharedFrame& s = GetSharedFrame();
    if (s.Ensure())
    {
        if (started)
        {
            s.hdr->req_width = w;
            s.hdr->req_height = h;
            s.hdr->req_fps = kFps;
        }
        s.hdr->consumer_heartbeat = FileTimeNow();
        s.hdr->consumer_active = n > 0 ? 1 : 0;
    }
    VLog(L"stream %s, active streams=%ld (req %ux%u)", started ? L"started" : L"stopped", n, w, h);
}

static HRESULT CreateNV12Type(UINT32 w, UINT32 h, IMFMediaType** out)
{
    IMFMediaType* t = nullptr;
    HRESULT hr = MFCreateMediaType(&t);
    if (FAILED(hr)) return hr;
    t->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    t->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    MFSetAttributeSize(t, MF_MT_FRAME_SIZE, w, h);
    MFSetAttributeRatio(t, MF_MT_FRAME_RATE, kFps, 1);
    MFSetAttributeRatio(t, MF_MT_FRAME_RATE_RANGE_MAX, kFps, 1);
    MFSetAttributeRatio(t, MF_MT_FRAME_RATE_RANGE_MIN, kFps, 1);
    MFSetAttributeRatio(t, MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    t->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    t->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);
    t->SetUINT32(MF_MT_FIXED_SIZE_SAMPLES, TRUE);
    t->SetUINT32(MF_MT_DEFAULT_STRIDE, w);
    t->SetUINT32(MF_MT_SAMPLE_SIZE, w * h * 3 / 2);
    t->SetUINT32(MF_MT_AVG_BITRATE, w * h * 3 / 2 * 8 * kFps);
    *out = t;
    return S_OK;
}

MediaStream::MediaStream()
{
    InterlockedIncrement(&g_objectCount);
    MFCreateAttributes(&m_attr, 8);
}

MediaStream::~MediaStream()
{
    Shutdown();
    if (m_attr) m_attr->Release();
    if (m_wake) CloseHandle(m_wake);
    if (m_timer) CloseHandle(m_timer);
    InterlockedDecrement(&g_objectCount);
}

HRESULT MediaStream::Initialize(MediaSource* source, DWORD id)
{
    if (!m_attr) return E_OUTOFMEMORY;
    m_id = id;
    m_source = source;
    m_source->AddRef(); // released on Shutdown (cycle broken there, as in VCamSample)
    m_wake = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    m_timer = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    if (!m_timer) m_timer = CreateWaitableTimerExW(nullptr, nullptr, 0, TIMER_ALL_ACCESS);

    HRESULT hr;
    if (FAILED(hr = SetGUID(MF_DEVICESTREAM_STREAM_CATEGORY, PINNAME_VIDEO_CAPTURE))) return hr;
    if (FAILED(hr = SetUINT32(MF_DEVICESTREAM_STREAM_ID, id))) return hr;
    if (FAILED(hr = SetUINT32(MF_DEVICESTREAM_FRAMESERVER_SHARED, 1))) return hr;
    if (FAILED(hr = SetUINT32(MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES, MFFrameSourceTypes_Color))) return hr;
    if (FAILED(hr = MFCreateEventQueue(&m_queue))) return hr;

    IMFMediaType* types[2] = {};
    if (FAILED(hr = CreateNV12Type(1920, 1080, &types[0]))) return hr;
    if (FAILED(hr = CreateNV12Type(1280, 720, &types[1]))) { types[0]->Release(); return hr; }
    hr = MFCreateStreamDescriptor(id, 2, types, &m_desc);
    if (SUCCEEDED(hr))
    {
        IMFMediaTypeHandler* h = nullptr;
        hr = m_desc->GetMediaTypeHandler(&h);
        if (SUCCEEDED(hr)) { hr = h->SetCurrentMediaType(types[0]); h->Release(); }
        // the descriptor's own attributes need the device stream attributes too
        m_desc->SetGUID(MF_DEVICESTREAM_STREAM_CATEGORY, PINNAME_VIDEO_CAPTURE);
        m_desc->SetUINT32(MF_DEVICESTREAM_STREAM_ID, id);
        m_desc->SetUINT32(MF_DEVICESTREAM_FRAMESERVER_SHARED, 1);
        m_desc->SetUINT32(MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES, MFFrameSourceTypes_Color);
    }
    types[0]->Release();
    types[1]->Release();
    return hr;
}

HRESULT MediaStream::SetAllocator(IUnknown* allocator)
{
    if (!allocator) return E_POINTER;
    AcquireSRWLockExclusive(&m_lock);
    if (m_alloc) { m_alloc->Release(); m_alloc = nullptr; }
    HRESULT hr = allocator->QueryInterface(IID_PPV_ARGS(&m_alloc));
    ReleaseSRWLockExclusive(&m_lock);
    VLog(L"stream SetAllocator hr=0x%08X", hr);
    return hr;
}

HRESULT MediaStream::Start(IMFMediaType* type)
{
    AcquireSRWLockExclusive(&m_lock);
    HRESULT hr = S_OK;
    if (!m_queue) { ReleaseSRWLockExclusive(&m_lock); return MF_E_SHUTDOWN; }

    if (type)
    {
        if (m_type) m_type->Release();
        m_type = type; m_type->AddRef();
    }
    if (!m_type)
    {
        IMFMediaTypeHandler* h = nullptr;
        if (SUCCEEDED(m_desc->GetMediaTypeHandler(&h))) { h->GetCurrentMediaType(&m_type); h->Release(); }
    }
    if (m_type)
    {
        UINT32 w = 0, hh = 0;
        if (SUCCEEDED(MFGetAttributeSize(m_type, MF_MT_FRAME_SIZE, &w, &hh)) && w && hh) { m_w = w; m_h = hh; }
    }

    if (!m_alloc)
    {
        hr = MFCreateVideoSampleAllocatorEx(IID_PPV_ARGS(&m_alloc));
        VLog(L"stream: no provided allocator, created own hr=0x%08X", hr);
    }
    if (SUCCEEDED(hr) && m_alloc)
    {
        m_alloc->UninitializeSampleAllocator();
        hr = m_alloc->InitializeSampleAllocator(10, m_type);
        if (FAILED(hr)) VLog(L"InitializeSampleAllocator failed 0x%08X", hr);
    }
    bool wasRunning = m_state == MF_STREAM_STATE_RUNNING;
    if (SUCCEEDED(hr))
    {
        BuildPlaceholder();
        SharedFrame& s = GetSharedFrame();
        m_lastCounter = s.Ensure() ? s.hdr->frame_counter : 0;
        m_lastFreshTick = 0;
        m_state = MF_STREAM_STATE_RUNNING;
        if (!m_thread)
        {
            m_stopWorker = false;
            AddRef();
            m_thread = CreateThread(nullptr, 0, WorkerProc, this, 0, nullptr);
            if (!m_thread) { Release(); hr = HRESULT_FROM_WIN32(GetLastError()); }
        }
        if (SUCCEEDED(hr)) hr = m_queue->QueueEventParamVar(MEStreamStarted, GUID_NULL, S_OK, nullptr);
    }
    UINT32 w = m_w, h = m_h;
    ReleaseSRWLockExclusive(&m_lock);
    if (SUCCEEDED(hr) && !wasRunning) UpdateConsumerActive(true, w, h);
    VLog(L"MediaStream::Start %ux%u hr=0x%08X", w, h, hr);
    return hr;
}

void MediaStream::StopWorker()
{
    HANDLE t = nullptr;
    AcquireSRWLockExclusive(&m_lock);
    t = m_thread; m_thread = nullptr;
    m_stopWorker = true;
    ReleaseSRWLockExclusive(&m_lock);
    if (t)
    {
        SetEvent(m_wake);
        if (GetCurrentThreadId() != GetThreadId(t)) WaitForSingleObject(t, 5000);
        CloseHandle(t);
    }
    AcquireSRWLockExclusive(&m_lock);
    for (auto* tok : m_tokens) if (tok) tok->Release();
    m_tokens.clear();
    ReleaseSRWLockExclusive(&m_lock);
}

HRESULT MediaStream::Stop()
{
    bool wasRunning = m_state != MF_STREAM_STATE_STOPPED;
    StopWorker();
    AcquireSRWLockExclusive(&m_lock);
    HRESULT hr = S_OK;
    if (!m_queue) hr = MF_E_SHUTDOWN;
    else
    {
        if (m_alloc) m_alloc->UninitializeSampleAllocator();
        m_state = MF_STREAM_STATE_STOPPED;
        hr = m_queue->QueueEventParamVar(MEStreamStopped, GUID_NULL, S_OK, nullptr);
    }
    UINT32 w = m_w, h = m_h;
    ReleaseSRWLockExclusive(&m_lock);
    if (wasRunning) UpdateConsumerActive(false, w, h);
    VLog(L"MediaStream::Stop hr=0x%08X sent=%llu live=%llu", hr, m_framesSent, m_framesLive);
    return hr;
}

void MediaStream::Shutdown()
{
    bool wasRunning = m_state != MF_STREAM_STATE_STOPPED;
    StopWorker();
    AcquireSRWLockExclusive(&m_lock);
    if (m_queue) { m_queue->Shutdown(); m_queue->Release(); m_queue = nullptr; }
    if (m_desc) { m_desc->Release(); m_desc = nullptr; }
    if (m_alloc) { m_alloc->UninitializeSampleAllocator(); m_alloc->Release(); m_alloc = nullptr; }
    if (m_type) { m_type->Release(); m_type = nullptr; }
    IMFMediaSource* src = m_source; m_source = nullptr;
    m_state = MF_STREAM_STATE_STOPPED;
    UINT32 w = m_w, h = m_h;
    ReleaseSRWLockExclusive(&m_lock);
    if (wasRunning) UpdateConsumerActive(false, w, h);
    if (src) src->Release();
}

// ---- frame production ----

void MediaStream::BuildPlaceholder()
{
    if (m_phW == m_w && m_phH == m_h && !m_placeholder.empty()) return;
    const UINT32 w = m_w, h = m_h;
    m_placeholder.assign((size_t)w * h * 3 / 2, 0);
    BYTE* y = m_placeholder.data();
    BYTE* uv = y + (size_t)w * h;
    memset(y, 40, (size_t)w * h);           // dark gray
    memset(uv, 128, (size_t)w * h / 2);     // neutral chroma

    // centered camera glyph: rounded body, viewfinder bump, lens rings
    const int cx = w / 2, cy = h / 2;
    const int bw = h * 2 / 5, bh = h * 4 / 15;          // body half-size base
    const int left = cx - bw / 2 - bw / 4, right = cx + bw / 2 + bw / 4;
    const int top = cy - bh / 2 - bh / 4, bottom = cy + bh / 2 + bh / 4;
    const int rad = h / 40 + 1;
    const int lensR = (bottom - top) * 3 / 10;
    for (int j = 0; j < (int)h; j++)
    {
        for (int i = 0; i < (int)w; i++)
        {
            int v = -1;
            // body with rounded corners
            if (i >= left && i < right && j >= top && j < bottom)
            {
                int dx = 0, dy = 0;
                if (i < left + rad) dx = left + rad - i; else if (i >= right - rad) dx = i - (right - rad - 1);
                if (j < top + rad) dy = top + rad - j; else if (j >= bottom - rad) dy = j - (bottom - rad - 1);
                if (dx * dx + dy * dy <= rad * rad) v = 95;
            }
            // viewfinder bump
            int bl = cx - (right - left) / 6, br2 = cx + (right - left) / 6;
            if (i >= bl && i < br2 && j >= top - (bottom - top) / 6 && j < top) v = 95;
            // lens
            int ddx = i - cx, ddy = j - cy;
            int d2 = ddx * ddx + ddy * ddy;
            if (d2 <= lensR * lensR && v >= 0)
            {
                int inner = lensR * 3 / 4, core = lensR / 3;
                v = d2 <= core * core ? 120 : (d2 <= inner * inner ? 40 : 160);
            }
            if (v >= 0) y[(size_t)j * w + i] = (BYTE)v;
        }
    }
    m_phW = w; m_phH = h;
}

HRESULT MediaStream::FillBuffer(IMFSample* sample)
{
    IMFMediaBuffer* buf = nullptr;
    HRESULT hr = sample->GetBufferByIndex(0, &buf);
    if (FAILED(hr)) return hr;

    const UINT32 w = m_w, h = m_h;
    const size_t frameBytes = (size_t)w * h * 3 / 2;

    // choose source: live frame from shared memory or placeholder
    const BYTE* src = m_placeholder.data();
    bool live = false;
    SharedFrame& s = GetSharedFrame();
    if (s.Ensure() && s.hdr->magic == PSVC_MAGIC)
    {
        uint64_t c = s.hdr->frame_counter;
        ULONGLONG tick = GetTickCount64();
        if (c != m_lastCounter) { m_lastCounter = c; m_lastFreshTick = tick; }
        if (m_lastFreshTick && tick - m_lastFreshTick <= 1000 &&
            s.hdr->format == PSVC_FORMAT_NV12 && s.hdr->width == w && s.hdr->height == h &&
            frameBytes <= PSVC_FRAME_CAPACITY)
        {
            src = s.data;
            live = true;
        }
    }
    if (live != m_wasLive)
    {
        VLog(L"stream: switching to %s frames (%ux%u)", live ? L"LIVE" : L"PLACEHOLDER", w, h);
        m_wasLive = live;
    }

    IMF2DBuffer2* b2 = nullptr;
    if (SUCCEEDED(buf->QueryInterface(IID_PPV_ARGS(&b2))))
    {
        BYTE* scan0 = nullptr; LONG pitch = 0; BYTE* start = nullptr; DWORD len = 0;
        hr = b2->Lock2DSize(MF2DBuffer_LockFlags_Write, &scan0, &pitch, &start, &len);
        if (SUCCEEDED(hr))
        {
            if (pitch == (LONG)w)
                memcpy(scan0, src, frameBytes);
            else
            {
                for (UINT32 j = 0; j < h; j++) memcpy(scan0 + (LONGLONG)j * pitch, src + (size_t)j * w, w);
                BYTE* uvDst = scan0 + (LONGLONG)pitch * h;
                const BYTE* uvSrc = src + (size_t)w * h;
                for (UINT32 j = 0; j < h / 2; j++) memcpy(uvDst + (LONGLONG)j * pitch, uvSrc + (size_t)j * w, w);
            }
            b2->Unlock2D();
        }
        b2->Release();
    }
    else
    {
        BYTE* p = nullptr; DWORD max = 0;
        hr = buf->Lock(&p, &max, nullptr);
        if (SUCCEEDED(hr))
        {
            memcpy(p, src, min((size_t)max, frameBytes));
            buf->Unlock();
            buf->SetCurrentLength((DWORD)min((size_t)max, frameBytes));
        }
    }
    buf->Release();
    if (SUCCEEDED(hr) && live) m_framesLive++;
    return hr;
}

HRESULT MediaStream::DeliverSample(IUnknown* token)
{
    AcquireSRWLockExclusive(&m_lock);
    HRESULT hr = S_OK;
    if (!m_queue || !m_alloc || m_state != MF_STREAM_STATE_RUNNING) hr = MF_E_SHUTDOWN;
    IMFSample* sample = nullptr;
    if (SUCCEEDED(hr)) hr = m_alloc->AllocateSample(&sample);
    if (SUCCEEDED(hr)) hr = FillBuffer(sample);
    if (SUCCEEDED(hr))
    {
        sample->SetSampleTime(MFGetSystemTime());
        sample->SetSampleDuration(kFrameDuration);
        sample->SetUINT32(MFSampleExtension_CleanPoint, 1);
        if (token) sample->SetUnknown(MFSampleExtension_Token, token);
        hr = m_queue->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample);
        m_framesSent++;
    }
    if (sample) sample->Release();
    ReleaseSRWLockExclusive(&m_lock);
    if (FAILED(hr) && hr != MF_E_SHUTDOWN)
    {
        static ULONGLONG lastLog = 0;
        if (GetTickCount64() - lastLog > 2000) { lastLog = GetTickCount64(); VLog(L"DeliverSample failed 0x%08X", hr); }
    }
    return hr;
}

DWORD WINAPI MediaStream::WorkerProc(LPVOID p)
{
    MediaStream* self = (MediaStream*)p;
    HRESULT hrCo = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    self->WorkerLoop();
    if (SUCCEEDED(hrCo)) CoUninitialize();
    self->Release();
    return 0;
}

void MediaStream::WorkerLoop()
{
    VLog(L"worker started");
    while (!m_stopWorker)
    {
        // heartbeat
        ULONGLONG tick = GetTickCount64();
        if (tick - m_lastHeartbeat >= 500)
        {
            m_lastHeartbeat = tick;
            SharedFrame& s = GetSharedFrame();
            if (s.Ensure())
            {
                s.hdr->consumer_heartbeat = FileTimeNow();
                s.hdr->consumer_active = g_activeStreams > 0 ? 1 : 0;
                s.hdr->req_width = m_w;
                s.hdr->req_height = m_h;
            }
        }

        AcquireSRWLockShared(&m_lock);
        bool pending = !m_tokens.empty();
        ReleaseSRWLockShared(&m_lock);
        if (!pending)
        {
            WaitForSingleObject(m_wake, 200);
            continue;
        }

        // pace to ~30 fps, but deliver early if the producer just published a frame
        LONGLONG now = Now100ns();
        LONGLONG due = m_nextDue;
        if (due - now > kFrameDuration || now - due > kFrameDuration) due = now; // (re)sync schedule
        if (now < due)
        {
            HANDLE hs[3]; DWORD n = 0;
            hs[n++] = m_wake;
            HANDLE ev = GetSharedFrame().frameEvent;
            if (ev) hs[n++] = ev;
            DWORD ms = (DWORD)((due - now) / 10000) + 1;
            if (m_timer)
            {
                LARGE_INTEGER li; li.QuadPart = -(due - now);
                SetWaitableTimer(m_timer, &li, 0, nullptr, nullptr, FALSE);
                hs[n++] = m_timer;
                ms = INFINITE;
            }
            DWORD r = WaitForMultipleObjects(n, hs, FALSE, ms);
            if (m_stopWorker) break;
            if (r == WAIT_OBJECT_0) { m_nextDue = due; continue; } // new request/stop: re-evaluate
            if (ev && r == WAIT_OBJECT_0 + 1 && Now100ns() - m_lastDeliver < kFrameDuration * 6 / 10)
            { m_nextDue = due; continue; } // fresh frame but too early: keep waiting
        }

        m_nextDue = max(due, Now100ns() - kFrameDuration / 2) + kFrameDuration;

        IUnknown* token = nullptr;
        AcquireSRWLockExclusive(&m_lock);
        bool have = !m_tokens.empty();
        if (have) { token = m_tokens.front(); m_tokens.pop_front(); }
        ReleaseSRWLockExclusive(&m_lock);
        if (!have) continue;

        LONGLONG t0 = Now100ns();
        LONGLONG gap = t0 - m_lastDeliver;
        m_lastDeliver = t0;
        DeliverSample(token);
        if (token) token->Release();
        m_statFill += Now100ns() - t0;
        m_statGap += gap;
        if (++m_statN == (m_statN0 ? 300 : 30))
        {
            VLog(L"stats: %.1f fps delivered, avg fill %.2f ms, sent=%llu live=%llu",
                10000000.0 * m_statN / (double)m_statGap, m_statFill / 10000.0 / m_statN, m_framesSent, m_framesLive);
            m_statN = 0; m_statFill = 0; m_statGap = 0; m_statN0 = true;
        }
    }
    VLog(L"worker exit");
}

// ---- IUnknown ----
STDMETHODIMP MediaStream::QueryInterface(REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == __uuidof(IMFMediaEventGenerator) || riid == __uuidof(IMFMediaStream) || riid == __uuidof(IMFMediaStream2))
        *ppv = static_cast<IMFMediaStream2*>(this);
    else if (riid == __uuidof(IMFAttributes))
        *ppv = static_cast<IMFAttributes*>(this);
    else if (riid == __uuidof(IKsControl))
        *ppv = static_cast<IKsControl*>(this);
    else
        return E_NOINTERFACE;
    AddRef();
    return S_OK;
}
STDMETHODIMP_(ULONG) MediaStream::AddRef() { return InterlockedIncrement(&m_ref); }
STDMETHODIMP_(ULONG) MediaStream::Release()
{
    LONG r = InterlockedDecrement(&m_ref);
    if (!r) delete this;
    return r;
}

// ---- IMFMediaEventGenerator ----
#define STREAM_QUEUE_CALL(expr) \
    AcquireSRWLockShared(&m_lock); \
    HRESULT hr = m_queue ? (expr) : MF_E_SHUTDOWN; \
    ReleaseSRWLockShared(&m_lock); \
    return hr;

STDMETHODIMP MediaStream::BeginGetEvent(IMFAsyncCallback* cb, IUnknown* st) { STREAM_QUEUE_CALL(m_queue->BeginGetEvent(cb, st)) }
STDMETHODIMP MediaStream::EndGetEvent(IMFAsyncResult* r, IMFMediaEvent** ev) { STREAM_QUEUE_CALL(m_queue->EndGetEvent(r, ev)) }
STDMETHODIMP MediaStream::GetEvent(DWORD flags, IMFMediaEvent** ev)
{
    // may block: don't hold the lock
    AcquireSRWLockShared(&m_lock);
    IMFMediaEventQueue* q = m_queue; if (q) q->AddRef();
    ReleaseSRWLockShared(&m_lock);
    if (!q) return MF_E_SHUTDOWN;
    HRESULT hr = q->GetEvent(flags, ev);
    q->Release();
    return hr;
}
STDMETHODIMP MediaStream::QueueEvent(MediaEventType met, REFGUID ext, HRESULT st, const PROPVARIANT* v) { STREAM_QUEUE_CALL(m_queue->QueueEventParamVar(met, ext, st, v)) }

// ---- IMFMediaStream ----
STDMETHODIMP MediaStream::GetMediaSource(IMFMediaSource** pp)
{
    if (!pp) return E_POINTER;
    AcquireSRWLockShared(&m_lock);
    HRESULT hr = MF_E_SHUTDOWN;
    *pp = nullptr;
    if (m_source) { *pp = m_source; m_source->AddRef(); hr = S_OK; }
    ReleaseSRWLockShared(&m_lock);
    return hr;
}

STDMETHODIMP MediaStream::GetStreamDescriptor(IMFStreamDescriptor** pp)
{
    if (!pp) return E_POINTER;
    AcquireSRWLockShared(&m_lock);
    HRESULT hr = MF_E_SHUTDOWN;
    *pp = nullptr;
    if (m_desc) { *pp = m_desc; m_desc->AddRef(); hr = S_OK; }
    ReleaseSRWLockShared(&m_lock);
    return hr;
}

STDMETHODIMP MediaStream::RequestSample(IUnknown* token)
{
    AcquireSRWLockExclusive(&m_lock);
    HRESULT hr = S_OK;
    if (!m_queue) hr = MF_E_SHUTDOWN;
    else if (m_state != MF_STREAM_STATE_RUNNING) hr = MF_E_INVALIDREQUEST;
    else
    {
        if (token) token->AddRef();
        m_tokens.push_back(token);
        if (m_tokens.size() > 16) { IUnknown* t = m_tokens.front(); m_tokens.pop_front(); if (t) t->Release(); }
    }
    ReleaseSRWLockExclusive(&m_lock);
    if (SUCCEEDED(hr)) SetEvent(m_wake);
    return hr;
}

// ---- IMFMediaStream2 ----
STDMETHODIMP MediaStream::SetStreamState(MF_STREAM_STATE value)
{
    VLog(L"MediaStream::SetStreamState %d -> %d", m_state, value);
    if (value == m_state) return S_OK;
    switch (value)
    {
    case MF_STREAM_STATE_PAUSED:
        if (m_state != MF_STREAM_STATE_RUNNING) return MF_E_INVALID_STATE_TRANSITION;
        m_state = value;
        return S_OK;
    case MF_STREAM_STATE_RUNNING:
        return Start(nullptr);
    case MF_STREAM_STATE_STOPPED:
        return Stop();
    default:
        return MF_E_INVALID_STATE_TRANSITION;
    }
}

STDMETHODIMP MediaStream::GetStreamState(MF_STREAM_STATE* value)
{
    if (!value) return E_POINTER;
    *value = m_state;
    return S_OK;
}

// ---- IKsControl ----
STDMETHODIMP MediaStream::KsProperty(PKSPROPERTY p, ULONG, LPVOID, ULONG, ULONG* br)
{
    if (!p || !br) return E_POINTER;
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}
STDMETHODIMP MediaStream::KsMethod(PKSMETHOD m, ULONG, LPVOID, ULONG, ULONG* br)
{
    if (!m || !br) return E_POINTER;
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}
STDMETHODIMP MediaStream::KsEvent(PKSEVENT, ULONG, LPVOID, ULONG, ULONG* br)
{
    if (!br) return E_POINTER;
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}


