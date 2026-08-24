#ifndef ADOBE_CONNECT_MYCLIENT_H_
#define ADOBE_CONNECT_MYCLIENT_H_

#include "include/cef_client.h"

class MyClient : public CefClient, public CefLifeSpanHandler
{
public:
    MyClient() = default;

    // Without this, the CefLifeSpanHandler base is inert: CefClient's default
    // returns null, so none of its callbacks are ever delivered.
    CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override
    {
        return this;
    }

private:
    IMPLEMENT_REFCOUNTING(MyClient);
};

#endif  // ADOBE_CONNECT_MYCLIENT_H_
