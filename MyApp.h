#ifndef ADOBE_CONNECT_MYAPP_H_
#define ADOBE_CONNECT_MYAPP_H_

#include "include/cef_app.h"

class MyApp : public CefApp, public CefRenderProcessHandler
{
public:
    MyApp();
    CefRefPtr<CefRenderProcessHandler> GetRenderProcessHandler() override
    {
        return this;
    }
    void OnBeforeCommandLineProcessing(const CefString& process_type, CefRefPtr<CefCommandLine> command_line) override;
    IMPLEMENT_REFCOUNTING(MyApp);
};

#endif  // ADOBE_CONNECT_MYAPP_H_
