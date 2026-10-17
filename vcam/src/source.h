// PhoneSync virtual camera media source.
// Structure adapted from smourier/VCamSample (MIT License, Copyright (c) 2024 Simon Mourier):
// https://github.com/smourier/VCamSample  -- rewritten without WIL/C++WinRT, CPU-only NV12, shared-memory frames.
#pragma once
#include "common.h"
#include "attributes.h"
#include <mferror.h>
#include <mfvirtualcamera.h>
#include <propvarutil.h>
#include <ks.h>
#include <ksproxy.h>
#include <ksmedia.h>
#include <deque>
#include <vector>

extern volatile LONG g_objectCount;

class MediaSource;

class MediaStream : public IMFMediaStream2, public IMFAttributes, public IKsControl
{
public:
    MediaStream();
    HRESULT Initialize(MediaSource* source, DWORD id);
    HRESULT Start(IMFMediaType* type);
    HRESULT Stop();
    void Shutdown();
    HRESULT SetAllocator(IUnknown* allocator);

    // IUnknown
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    STDMETHODIMP_(ULONG) AddRef() override;
    STDMETHODIMP_(ULONG) Release() override;

    // IMFMediaEventGenerator
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* cb, IUnknown* state) override;
    STDMETHODIMP EndGetEvent(IMFAsyncResult* res, IMFMediaEvent** ev) override;
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** ev) override;
    STDMETHODIMP QueueEvent(MediaEventType met, REFGUID ext, HRESULT hr, const PROPVARIANT* v) override;

    // IMFMediaStream
    STDMETHODIMP GetMediaSource(IMFMediaSource** ppSource) override;
    STDMETHODIMP GetStreamDescriptor(IMFStreamDescriptor** ppDesc) override;
    STDMETHODIMP RequestSample(IUnknown* token) override;

    // IMFMediaStream2
    STDMETHODIMP SetStreamState(MF_STREAM_STATE value) override;
    STDMETHODIMP GetStreamState(MF_STREAM_STATE* value) override;

    // IKsControl
    STDMETHODIMP KsProperty(PKSPROPERTY p, ULONG pl, LPVOID d, ULONG dl, ULONG* br) override;
    STDMETHODIMP KsMethod(PKSMETHOD m, ULONG ml, LPVOID d, ULONG dl, ULONG* br) override;
    STDMETHODIMP KsEvent(PKSEVENT e, ULONG el, LPVOID d, ULONG dl, ULONG* br) override;

    IMPLEMENT_MFATTRIBUTES_DELEGATE

private:
    ~MediaStream();
    static DWORD WINAPI WorkerProc(LPVOID p);
    void WorkerLoop();
    HRESULT DeliverSample(IUnknown* token);
    HRESULT FillBuffer(IMFSample* sample);
    void BuildPlaceholder();
    void StopWorker();

    LONG m_ref = 1;
    IMFAttributes* m_attr = nullptr;
    SRWLOCK m_lock = SRWLOCK_INIT;
    MF_STREAM_STATE m_state = MF_STREAM_STATE_STOPPED;
    DWORD m_id = 0;
    IMFMediaEventQueue* m_queue = nullptr;
    IMFStreamDescriptor* m_desc = nullptr;
    IMFMediaSource* m_source = nullptr;
    IMFVideoSampleAllocatorEx* m_alloc = nullptr;
    IMFMediaType* m_type = nullptr;
    UINT32 m_w = 1920, m_h = 1080;

    // worker
    HANDLE m_thread = nullptr;
    HANDLE m_wake = nullptr;      // auto-reset: new request / stop
    volatile bool m_stopWorker = false;
    std::deque<IUnknown*> m_tokens;   // pending RequestSample tokens (may contain nullptr)
    LONGLONG m_lastDeliver = 0;       // QPC-based 100ns
    LONGLONG m_nextDue = 0;
    LONGLONG m_statFill = 0, m_statGap = 0; int m_statN = 0; bool m_statN0 = false;
    HANDLE m_timer = nullptr;         // high-resolution waitable timer for pacing
    uint64_t m_lastCounter = 0;
    ULONGLONG m_lastFreshTick = 0;
    ULONGLONG m_lastHeartbeat = 0;
    uint64_t m_framesSent = 0, m_framesLive = 0;
    bool m_wasLive = false;
    std::vector<BYTE> m_placeholder;
    UINT32 m_phW = 0, m_phH = 0;
};

class MediaSource : public IMFMediaSourceEx, public IMFAttributes, public IMFGetService, public IKsControl, public IMFSampleAllocatorControl
{
public:
    MediaSource();
    HRESULT Initialize(IMFAttributes* attributes);

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    STDMETHODIMP_(ULONG) AddRef() override;
    STDMETHODIMP_(ULONG) Release() override;

    // IMFMediaEventGenerator
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* cb, IUnknown* state) override;
    STDMETHODIMP EndGetEvent(IMFAsyncResult* res, IMFMediaEvent** ev) override;
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** ev) override;
    STDMETHODIMP QueueEvent(MediaEventType met, REFGUID ext, HRESULT hr, const PROPVARIANT* v) override;

    // IMFMediaSource
    STDMETHODIMP CreatePresentationDescriptor(IMFPresentationDescriptor** ppPD) override;
    STDMETHODIMP GetCharacteristics(DWORD* pdw) override;
    STDMETHODIMP Pause() override;
    STDMETHODIMP Shutdown() override;
    STDMETHODIMP Start(IMFPresentationDescriptor* pd, const GUID* fmt, const PROPVARIANT* pos) override;
    STDMETHODIMP Stop() override;

    // IMFMediaSourceEx
    STDMETHODIMP GetSourceAttributes(IMFAttributes** ppAttr) override;
    STDMETHODIMP GetStreamAttributes(DWORD id, IMFAttributes** ppAttr) override;
    STDMETHODIMP SetD3DManager(IUnknown* mgr) override;

    // IMFGetService
    STDMETHODIMP GetService(REFGUID guid, REFIID riid, LPVOID* ppv) override;

    // IMFSampleAllocatorControl
    STDMETHODIMP SetDefaultAllocator(DWORD outId, IUnknown* alloc) override;
    STDMETHODIMP GetAllocatorUsage(DWORD outId, DWORD* inId, MFSampleAllocatorUsage* usage) override;

    // IKsControl
    STDMETHODIMP KsProperty(PKSPROPERTY p, ULONG pl, LPVOID d, ULONG dl, ULONG* br) override;
    STDMETHODIMP KsMethod(PKSMETHOD m, ULONG ml, LPVOID d, ULONG dl, ULONG* br) override;
    STDMETHODIMP KsEvent(PKSEVENT e, ULONG el, LPVOID d, ULONG dl, ULONG* br) override;

    IMPLEMENT_MFATTRIBUTES_DELEGATE

private:
    ~MediaSource();
    LONG m_ref = 1;
    IMFAttributes* m_attr = nullptr;
    SRWLOCK m_lock = SRWLOCK_INIT;
    MediaStream* m_stream = nullptr;
    IMFMediaEventQueue* m_queue = nullptr;
    IMFPresentationDescriptor* m_pd = nullptr;
};

class Activator : public IMFActivate
{
public:
    Activator();
    HRESULT Initialize();

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    STDMETHODIMP_(ULONG) AddRef() override;
    STDMETHODIMP_(ULONG) Release() override;

    STDMETHODIMP ActivateObject(REFIID riid, void** ppv) override;
    STDMETHODIMP ShutdownObject() override;
    STDMETHODIMP DetachObject() override;

    IMPLEMENT_MFATTRIBUTES_DELEGATE

private:
    ~Activator();
    LONG m_ref = 1;
    IMFAttributes* m_attr = nullptr;
    SRWLOCK m_lock = SRWLOCK_INIT;
    MediaSource* m_source = nullptr;
};

void GuidToString(REFGUID g, wchar_t* buf, size_t n);




