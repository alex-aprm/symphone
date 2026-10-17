// IMFAttributes delegation helper (idea from smourier/VCamSample CBaseAttributes, MIT).
#pragma once
#include <mfapi.h>
#include <mfidl.h>

// Put inside a class that has a member `IMFAttributes* m_attr;`
#define IMPLEMENT_MFATTRIBUTES_DELEGATE \
    STDMETHODIMP GetItem(REFGUID k, PROPVARIANT* v) override { return m_attr->GetItem(k, v); } \
    STDMETHODIMP GetItemType(REFGUID k, MF_ATTRIBUTE_TYPE* t) override { return m_attr->GetItemType(k, t); } \
    STDMETHODIMP CompareItem(REFGUID k, REFPROPVARIANT v, BOOL* r) override { return m_attr->CompareItem(k, v, r); } \
    STDMETHODIMP Compare(IMFAttributes* a, MF_ATTRIBUTES_MATCH_TYPE m, BOOL* r) override { return m_attr->Compare(a, m, r); } \
    STDMETHODIMP GetUINT32(REFGUID k, UINT32* v) override { return m_attr->GetUINT32(k, v); } \
    STDMETHODIMP GetUINT64(REFGUID k, UINT64* v) override { return m_attr->GetUINT64(k, v); } \
    STDMETHODIMP GetDouble(REFGUID k, double* v) override { return m_attr->GetDouble(k, v); } \
    STDMETHODIMP GetGUID(REFGUID k, GUID* v) override { return m_attr->GetGUID(k, v); } \
    STDMETHODIMP GetStringLength(REFGUID k, UINT32* l) override { return m_attr->GetStringLength(k, l); } \
    STDMETHODIMP GetString(REFGUID k, LPWSTR v, UINT32 s, UINT32* l) override { return m_attr->GetString(k, v, s, l); } \
    STDMETHODIMP GetAllocatedString(REFGUID k, LPWSTR* v, UINT32* l) override { return m_attr->GetAllocatedString(k, v, l); } \
    STDMETHODIMP GetBlobSize(REFGUID k, UINT32* s) override { return m_attr->GetBlobSize(k, s); } \
    STDMETHODIMP GetBlob(REFGUID k, UINT8* b, UINT32 s, UINT32* l) override { return m_attr->GetBlob(k, b, s, l); } \
    STDMETHODIMP GetAllocatedBlob(REFGUID k, UINT8** b, UINT32* s) override { return m_attr->GetAllocatedBlob(k, b, s); } \
    STDMETHODIMP GetUnknown(REFGUID k, REFIID r, LPVOID* p) override { return m_attr->GetUnknown(k, r, p); } \
    STDMETHODIMP SetItem(REFGUID k, REFPROPVARIANT v) override { return m_attr->SetItem(k, v); } \
    STDMETHODIMP DeleteItem(REFGUID k) override { return m_attr->DeleteItem(k); } \
    STDMETHODIMP DeleteAllItems() override { return m_attr->DeleteAllItems(); } \
    STDMETHODIMP SetUINT32(REFGUID k, UINT32 v) override { return m_attr->SetUINT32(k, v); } \
    STDMETHODIMP SetUINT64(REFGUID k, UINT64 v) override { return m_attr->SetUINT64(k, v); } \
    STDMETHODIMP SetDouble(REFGUID k, double v) override { return m_attr->SetDouble(k, v); } \
    STDMETHODIMP SetGUID(REFGUID k, REFGUID v) override { return m_attr->SetGUID(k, v); } \
    STDMETHODIMP SetString(REFGUID k, LPCWSTR v) override { return m_attr->SetString(k, v); } \
    STDMETHODIMP SetBlob(REFGUID k, const UINT8* b, UINT32 s) override { return m_attr->SetBlob(k, b, s); } \
    STDMETHODIMP SetUnknown(REFGUID k, IUnknown* u) override { return m_attr->SetUnknown(k, u); } \
    STDMETHODIMP LockStore() override { return m_attr->LockStore(); } \
    STDMETHODIMP UnlockStore() override { return m_attr->UnlockStore(); } \
    STDMETHODIMP GetCount(UINT32* c) override { return m_attr->GetCount(c); } \
    STDMETHODIMP GetItemByIndex(UINT32 i, GUID* k, PROPVARIANT* v) override { return m_attr->GetItemByIndex(i, k, v); } \
    STDMETHODIMP CopyAllItems(IMFAttributes* d) override { return m_attr->CopyAllItems(d); }
