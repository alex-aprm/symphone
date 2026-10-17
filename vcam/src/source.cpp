// MediaSource + Activator.
// Adapted from smourier/VCamSample MediaSource.cpp / Activator.cpp (MIT License, Copyright (c) 2024 Simon Mourier).
#include "source.h"
#include "shm.h"

void GuidToString(REFGUID g, wchar_t* buf, size_t n)
{
    StringFromGUID2(g, buf, (int)n);
}

// =============================== MediaSource ===============================

MediaSource::MediaSource()
{
    InterlockedIncrement(&g_objectCount);
    MFCreateAttributes(&m_attr, 8);
}

MediaSource::~MediaSource()
{
    if (m_stream) { m_stream->Shutdown(); m_stream->Release(); m_stream = nullptr; }
    if (m_queue) { m_queue->Shutdown(); m_queue->Release(); }
    if (m_pd) m_pd->Release();
    if (m_attr) m_attr->Release();
    InterlockedDecrement(&g_objectCount);
}

HRESULT MediaSource::Initialize(IMFAttributes* attributes)
{
    if (!m_attr) return E_OUTOFMEMORY;
    HRESULT hr;
    if (attributes && FAILED(hr = attributes->CopyAllItems(m_attr))) return hr;

    // sensor profiles (as VCamSample does)
    IMFSensorProfileCollection* coll = nullptr;
    if (SUCCEEDED(MFCreateSensorProfileCollection(&coll)))
    {
        IMFSensorProfile* prof = nullptr;
        if (SUCCEEDED(MFCreateSensorProfile(KSCAMERAPROFILE_Legacy, 0, nullptr, &prof)))
        {
            prof->AddProfileFilter(0, L"((RES==;FRT<=30,1;SUT==))");
            coll->AddProfile(prof);
            prof->Release();
        }
        m_attr->SetUnknown(MF_DEVICEMFT_SENSORPROFILE_COLLECTION, coll);
        coll->Release();
    }

    m_stream = new (std::nothrow) MediaStream();
    if (!m_stream) return E_OUTOFMEMORY;
    if (FAILED(hr = m_stream->Initialize(this, 0))) return hr;

    IMFStreamDescriptor* sd = nullptr;
    if (FAILED(hr = m_stream->GetStreamDescriptor(&sd))) return hr;
    hr = MFCreatePresentationDescriptor(1, &sd, &m_pd);
    sd->Release();
    if (FAILED(hr)) return hr;
    m_pd->SelectStream(0); // video stream selected by default, like a hardware camera
    if (FAILED(hr = MFCreateEventQueue(&m_queue))) return hr;

    // create the shared memory early so the producer can find it
    GetSharedFrame().Ensure();
    return S_OK;
}

STDMETHODIMP MediaSource::QueryInterface(REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == __uuidof(IMFMediaEventGenerator) || riid == __uuidof(IMFMediaSource) || riid == __uuidof(IMFMediaSourceEx))
        *ppv = static_cast<IMFMediaSourceEx*>(this);
    else if (riid == __uuidof(IMFAttributes))
        *ppv = static_cast<IMFAttributes*>(this);
    else if (riid == __uuidof(IMFGetService))
        *ppv = static_cast<IMFGetService*>(this);
    else if (riid == __uuidof(IKsControl))
        *ppv = static_cast<IKsControl*>(this);
    else if (riid == __uuidof(IMFSampleAllocatorControl))
        *ppv = static_cast<IMFSampleAllocatorControl*>(this);
    else
        return E_NOINTERFACE;
    AddRef();
    return S_OK;
}
STDMETHODIMP_(ULONG) MediaSource::AddRef() { return InterlockedIncrement(&m_ref); }
STDMETHODIMP_(ULONG) MediaSource::Release()
{
    LONG r = InterlockedDecrement(&m_ref);
    if (!r) delete this;
    return r;
}

// IMFMediaEventGenerator
STDMETHODIMP MediaSource::BeginGetEvent(IMFAsyncCallback* cb, IUnknown* st)
{
    AcquireSRWLockShared(&m_lock);
    HRESULT hr = m_queue ? m_queue->BeginGetEvent(cb, st) : MF_E_SHUTDOWN;
    ReleaseSRWLockShared(&m_lock);
    return hr;
}
STDMETHODIMP MediaSource::EndGetEvent(IMFAsyncResult* r, IMFMediaEvent** ev)
{
    AcquireSRWLockShared(&m_lock);
    HRESULT hr = m_queue ? m_queue->EndGetEvent(r, ev) : MF_E_SHUTDOWN;
    ReleaseSRWLockShared(&m_lock);
    return hr;
}
STDMETHODIMP MediaSource::GetEvent(DWORD flags, IMFMediaEvent** ev)
{
    AcquireSRWLockShared(&m_lock);
    IMFMediaEventQueue* q = m_queue; if (q) q->AddRef();
    ReleaseSRWLockShared(&m_lock);
    if (!q) return MF_E_SHUTDOWN;
    HRESULT hr = q->GetEvent(flags, ev);
    q->Release();
    return hr;
}
STDMETHODIMP MediaSource::QueueEvent(MediaEventType met, REFGUID ext, HRESULT st, const PROPVARIANT* v)
{
    AcquireSRWLockShared(&m_lock);
    HRESULT hr = m_queue ? m_queue->QueueEventParamVar(met, ext, st, v) : MF_E_SHUTDOWN;
    ReleaseSRWLockShared(&m_lock);
    return hr;
}

// IMFMediaSource
STDMETHODIMP MediaSource::CreatePresentationDescriptor(IMFPresentationDescriptor** pp)
{
    if (!pp) return E_POINTER;
    *pp = nullptr;
    AcquireSRWLockShared(&m_lock);
    HRESULT hr = m_pd ? m_pd->Clone(pp) : MF_E_SHUTDOWN;
    ReleaseSRWLockShared(&m_lock);
    return hr;
}

STDMETHODIMP MediaSource::GetCharacteristics(DWORD* pdw)
{
    if (!pdw) return E_POINTER;
    *pdw = MFMEDIASOURCE_IS_LIVE;
    return S_OK;
}

STDMETHODIMP MediaSource::Pause()
{
    return MF_E_INVALID_STATE_TRANSITION;
}

STDMETHODIMP MediaSource::Shutdown()
{
    VLog(L"MediaSource::Shutdown");
    AcquireSRWLockExclusive(&m_lock);
    if (!m_queue) { ReleaseSRWLockExclusive(&m_lock); return MF_E_SHUTDOWN; }
    MediaStream* stream = m_stream; m_stream = nullptr;
    m_queue->Shutdown(); m_queue->Release(); m_queue = nullptr;
    if (m_pd) { m_pd->Release(); m_pd = nullptr; }
    ReleaseSRWLockExclusive(&m_lock);
    if (stream) { stream->Shutdown(); stream->Release(); }
    return S_OK;
}

STDMETHODIMP MediaSource::Start(IMFPresentationDescriptor* pd, const GUID* fmt, const PROPVARIANT* pos)
{
    if (!pd || !pos) return E_POINTER;
    if (fmt && *fmt != GUID_NULL) return MF_E_UNSUPPORTED_TIME_FORMAT;

    AcquireSRWLockExclusive(&m_lock);
    HRESULT hr = S_OK;
    MediaStream* stream = m_stream;
    if (!m_queue || !m_pd || !stream) { ReleaseSRWLockExclusive(&m_lock); return MF_E_SHUTDOWN; }
    stream->AddRef();
    IMFMediaEventQueue* q = m_queue; q->AddRef();
    ReleaseSRWLockExclusive(&m_lock);

    DWORD count = 0;
    hr = pd->GetStreamDescriptorCount(&count);
    for (DWORD i = 0; SUCCEEDED(hr) && i < count; i++)
    {
        BOOL selected = FALSE;
        IMFStreamDescriptor* sd = nullptr;
        hr = pd->GetStreamDescriptorByIndex(i, &selected, &sd);
        if (FAILED(hr)) break;
        DWORD id = 0;
        sd->GetStreamIdentifier(&id);
        if (id != 0) { sd->Release(); continue; }

        MF_STREAM_STATE state;
        stream->GetStreamState(&state);
        VLog(L"MediaSource::Start stream id=%lu selected=%d state=%d", id, selected, state);
        if (selected)
        {
            m_pd->SelectStream(0);
            hr = q->QueueEventParamUnk(state == MF_STREAM_STATE_STOPPED ? MENewStream : MEUpdatedStream, GUID_NULL, S_OK,
                static_cast<IMFMediaStream2*>(stream));
            IMFMediaTypeHandler* h = nullptr;
            IMFMediaType* type = nullptr;
            if (SUCCEEDED(hr)) hr = sd->GetMediaTypeHandler(&h);
            if (SUCCEEDED(hr)) hr = h->GetCurrentMediaType(&type);
            if (SUCCEEDED(hr)) hr = stream->Start(type);
            if (type) type->Release();
            if (h) h->Release();
        }
        else if (state != MF_STREAM_STATE_STOPPED)
        {
            m_pd->DeselectStream(0);
            hr = stream->Stop();
        }
        sd->Release();
    }

    if (SUCCEEDED(hr))
    {
        PROPVARIANT time;
        InitPropVariantFromInt64(MFGetSystemTime(), &time);
        hr = q->QueueEventParamVar(MESourceStarted, GUID_NULL, S_OK, &time);
        PropVariantClear(&time);
    }
    q->Release();
    stream->Release();
    VLog(L"MediaSource::Start hr=0x%08X", hr);
    return hr;
}

STDMETHODIMP MediaSource::Stop()
{
    AcquireSRWLockExclusive(&m_lock);
    MediaStream* stream = m_stream;
    if (!m_queue || !stream) { ReleaseSRWLockExclusive(&m_lock); return MF_E_SHUTDOWN; }
    stream->AddRef();
    IMFMediaEventQueue* q = m_queue; q->AddRef();
    if (m_pd) m_pd->DeselectStream(0);
    ReleaseSRWLockExclusive(&m_lock);

    HRESULT hr = stream->Stop();
    PROPVARIANT time;
    InitPropVariantFromInt64(MFGetSystemTime(), &time);
    HRESULT hr2 = q->QueueEventParamVar(MESourceStopped, GUID_NULL, S_OK, &time);
    PropVariantClear(&time);
    q->Release();
    stream->Release();
    VLog(L"MediaSource::Stop hr=0x%08X", hr);
    return FAILED(hr) ? hr : hr2;
}

// IMFMediaSourceEx
STDMETHODIMP MediaSource::GetSourceAttributes(IMFAttributes** pp)
{
    if (!pp) return E_POINTER;
    *pp = static_cast<IMFAttributes*>(this);
    AddRef();
    return S_OK;
}

STDMETHODIMP MediaSource::GetStreamAttributes(DWORD id, IMFAttributes** pp)
{
    if (!pp) return E_POINTER;
    *pp = nullptr;
    AcquireSRWLockShared(&m_lock);
    HRESULT hr = S_OK;
    if (!m_stream) hr = MF_E_SHUTDOWN;
    else if (id != 0) hr = MF_E_INVALIDSTREAMNUMBER;
    else { *pp = static_cast<IMFAttributes*>(m_stream); m_stream->AddRef(); }
    ReleaseSRWLockShared(&m_lock);
    return hr;
}

STDMETHODIMP MediaSource::SetD3DManager(IUnknown* mgr)
{
    // CPU-only implementation: samples are system-memory NV12. Accept and ignore.
    VLog(L"MediaSource::SetD3DManager %p (ignored, CPU samples)", mgr);
    return S_OK;
}

// IMFGetService
STDMETHODIMP MediaSource::GetService(REFGUID, REFIID, LPVOID* ppv)
{
    if (ppv) *ppv = nullptr;
    return MF_E_UNSUPPORTED_SERVICE;
}

// IMFSampleAllocatorControl
STDMETHODIMP MediaSource::SetDefaultAllocator(DWORD outId, IUnknown* alloc)
{
    if (!alloc) return E_POINTER;
    AcquireSRWLockShared(&m_lock);
    MediaStream* s = m_stream; if (s) s->AddRef();
    ReleaseSRWLockShared(&m_lock);
    if (!s) return MF_E_SHUTDOWN;
    HRESULT hr = outId == 0 ? s->SetAllocator(alloc) : MF_E_INVALIDSTREAMNUMBER;
    s->Release();
    return hr;
}

STDMETHODIMP MediaSource::GetAllocatorUsage(DWORD outId, DWORD* inId, MFSampleAllocatorUsage* usage)
{
    if (!inId || !usage) return E_POINTER;
    if (outId != 0) return MF_E_INVALIDSTREAMNUMBER;
    *inId = outId;
    *usage = MFSampleAllocatorUsage_UsesProvidedAllocator;
    return S_OK;
}

// IKsControl
STDMETHODIMP MediaSource::KsProperty(PKSPROPERTY p, ULONG, LPVOID, ULONG, ULONG* br)
{
    if (!p || !br) return E_POINTER;
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}
STDMETHODIMP MediaSource::KsMethod(PKSMETHOD m, ULONG, LPVOID, ULONG, ULONG* br)
{
    if (!m || !br) return E_POINTER;
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}
STDMETHODIMP MediaSource::KsEvent(PKSEVENT, ULONG, LPVOID, ULONG, ULONG* br)
{
    if (!br) return E_POINTER;
    return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
}

// =============================== Activator ===============================

Activator::Activator()
{
    InterlockedIncrement(&g_objectCount);
    MFCreateAttributes(&m_attr, 4);
}

Activator::~Activator()
{
    if (m_source) m_source->Release();
    if (m_attr) m_attr->Release();
    InterlockedDecrement(&g_objectCount);
}

HRESULT Activator::Initialize()
{
    if (!m_attr) return E_OUTOFMEMORY;
    m_attr->SetUINT32(MF_VIRTUALCAMERA_PROVIDE_ASSOCIATED_CAMERA_SOURCES, 1);
    m_attr->SetGUID(MFT_TRANSFORM_CLSID_Attribute, CLSID_PhoneSyncVCam);
    m_source = new (std::nothrow) MediaSource();
    if (!m_source) return E_OUTOFMEMORY;
    return m_source->Initialize(m_attr);
}

STDMETHODIMP Activator::QueryInterface(REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == __uuidof(IMFAttributes) || riid == __uuidof(IMFActivate))
        *ppv = static_cast<IMFActivate*>(this);
    else
        return E_NOINTERFACE;
    AddRef();
    return S_OK;
}
STDMETHODIMP_(ULONG) Activator::AddRef() { return InterlockedIncrement(&m_ref); }
STDMETHODIMP_(ULONG) Activator::Release()
{
    LONG r = InterlockedDecrement(&m_ref);
    if (!r) delete this;
    return r;
}

STDMETHODIMP Activator::ActivateObject(REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    AcquireSRWLockExclusive(&m_lock);
    HRESULT hr = S_OK;
    if (!m_source)
    {
        m_source = new (std::nothrow) MediaSource();
        hr = m_source ? m_source->Initialize(m_attr) : E_OUTOFMEMORY;
    }
    if (SUCCEEDED(hr)) hr = m_source->QueryInterface(riid, ppv);
    ReleaseSRWLockExclusive(&m_lock);
    wchar_t g[64]; GuidToString(riid, g, 64);
    VLog(L"Activator::ActivateObject %s hr=0x%08X", g, hr);
    return hr;
}

STDMETHODIMP Activator::ShutdownObject()
{
    VLog(L"Activator::ShutdownObject");
    return S_OK;
}

STDMETHODIMP Activator::DetachObject()
{
    VLog(L"Activator::DetachObject");
    AcquireSRWLockExclusive(&m_lock);
    if (m_source) { m_source->Release(); m_source = nullptr; }
    ReleaseSRWLockExclusive(&m_lock);
    return S_OK;
}

