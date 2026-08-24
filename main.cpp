#include "include/cef_app.h"
#include "include/wrapper/cef_helpers.h"
#include "MyApp.h"
#include "MyClient.h"
#include <X11/X.h>
#include <X11/Xutil.h>
#include <chrono>
#include <cstring>
#include <iostream>
#include <pwd.h>
#include <thread>
#include <unistd.h>
using namespace std;

namespace {

CefRefPtr<CefBrowser> browser;
Display *main_display = nullptr;
Window window_xid = 0;
Window child_window = 0;
Atom wm_delete_message = 0;

bool isOpen = true;

// Roughly 60 Hz.  The original slept for ((1000 / 30) / 1000) seconds, which is
// integer arithmetic for sleep(0) - the process spun a core at 100% for the
// whole meeting.
constexpr auto kFrameDelay = chrono::milliseconds(16);

// Drain whatever X11 has queued without blocking.
//
// This used to run on a second thread blocked in XNextEvent.  That was a
// problem in three ways: Xlib was being called from two threads without
// XInitThreads, the thread tore the display down underneath the still-running
// CEF loop, and - because the std::thread was neither joined nor detached - its
// destructor called std::terminate when main returned, so a clean quit aborted.
// Polling from the one loop removes all three.
void pump_x_events()
{
    while (main_display && XPending(main_display))
    {
        XEvent event;
        XNextEvent(main_display, &event);

        if (event.type == ClientMessage)
        {
            if (static_cast<Atom>(event.xclient.data.l[0]) == wm_delete_message)
            {
                isOpen = false;
                return;
            }
        }
        else if (event.type == ConfigureNotify)
        {
            const auto ce = event.xconfigure;
            if (child_window)
                XResizeWindow(main_display, child_window, ce.width, ce.height);
            if (browser)
                browser->GetHost()->WasResized();
        }
        else if (event.type == MappingNotify)
        {
            XRefreshKeyboardMapping(&event.xmapping);
        }
    }
}

void maximizeWindow(Window win, Display *display)
{
    XEvent ev = {};
    ev.xclient.window = win;
    ev.xclient.type = ClientMessage;
    ev.xclient.format = 32;
    ev.xclient.message_type = XInternAtom(display, "_NET_WM_STATE", False);
    ev.xclient.data.l[0] = 1;
    ev.xclient.data.l[1] = XInternAtom(display, "_NET_WM_STATE_MAXIMIZED_HORZ", False);
    ev.xclient.data.l[2] = XInternAtom(display, "_NET_WM_STATE_MAXIMIZED_VERT", False);
    ev.xclient.data.l[3] = 1;

    XSendEvent(display, DefaultRootWindow(display), False,
               SubstructureRedirectMask | SubstructureNotifyMask, &ev);
}

// $HOME is what every other tool honours; getlogin() reports the owner of the
// controlling terminal, which is the wrong user under su/sudo and fails
// outright when there is no tty (launched from a .desktop file, for instance).
string user_config_dir()
{
    if (const char *home = getenv("HOME"); home && *home)
        return string(home) + "/.config/";

    if (const passwd *pw = getpwuid(getuid()); pw && pw->pw_dir && *pw->pw_dir)
        return string(pw->pw_dir) + "/.config/";

    return "/tmp/.config/";
}

}  // namespace

int main(int argc, char *argv[])
{
    CefMainArgs main_args(argc, argv);
    CefRefPtr<MyApp> cefapp(new MyApp);

    // CEF applications have multiple sub-processes (render, gpu, etc) that share
    // the same executable. This function checks the command-line and, if this is
    // a sub-process, executes the appropriate logic.
    int exit_code = CefExecuteProcess(main_args, cefapp.get(), nullptr);
    if (exit_code >= 0) {
        // The sub-process has completed so return here.
        return exit_code;
    }

    string launch_args = "";
    for (int i = 1; i < argc; ++i) {
        string arg = argv[i];
        if (arg.find("connectpro://") == 0) {
            launch_args = arg;
            break;
        }
    }

    if (launch_args.empty()) {
        cerr << "Error: No connectpro:// URL provided." << endl;
        cerr << "Usage: connect \"connectpro://YOUR_MEETING_URL\"" << endl;
        return 1;
    }

    CefWindowInfo window_info;
    CefSettings settings;
    CefBrowserSettings browser_settings = CefBrowserSettings();

    CefRefPtr<MyClient> cefclient(new MyClient);

    const string cache_path = user_config_dir();
    CefString(&settings.root_cache_path).FromString(cache_path);
    CefString(&settings.cache_path).FromString(cache_path + "adobe_connect/cache");

    settings.remote_debugging_port = 9450;
    settings.no_sandbox = 1;
    settings.log_severity = LOGSEVERITY_DEBUG;

    CefInitialize(main_args, settings, cefapp.get(), nullptr);

    CefRefPtr<CefRequestContext> ctx = CefRequestContext::GetGlobalContext();

    CefRefPtr<CefValue> val(CefValue::Create());
    val->SetInt(1);
    CefString err = CefString();
    ctx->SetPreference("profile.default_content_setting_values.plugins", val, err);
    ctx->SetPreference("plugins.run_all_flash_in_allow_mode", val, err);

    // Must precede every other Xlib call: CEF drives X from its own threads.
    XInitThreads();

    main_display = XOpenDisplay(nullptr);
    if (!main_display) {
        cerr << "Error: cannot open an X display. This app needs a running X "
                "session (under Wayland, an XWayland one)." << endl;
        CefShutdown();
        return 1;
    }

    Window root_window = XDefaultRootWindow(main_display);
    window_xid = XCreateWindow(main_display, root_window, 10, 10, 800, 600, 10, CopyFromParent, InputOutput, CopyFromParent, 0, nullptr);
    XMapWindow(main_display, window_xid);
    XFlush(main_display);
    XSelectInput(main_display, window_xid, StructureNotifyMask | PropertyChangeMask | SubstructureNotifyMask);
    XStoreName(main_display, window_xid, "Adobe Connect");

    wm_delete_message = XInternAtom(main_display, "WM_DELETE_WINDOW", False);
    XSetWMProtocols(main_display, window_xid, &wm_delete_message, 1);

    window_info.SetAsChild(window_xid, CefRect(0, 0, 800, 600));

    browser_settings.application_cache = cef_state_t::STATE_ENABLED;
    CefString(&window_info.window_name).FromString("Adobe Connect");

    browser = CefBrowserHost::CreateBrowserSync(window_info, cefclient, launch_args.substr(11), browser_settings, nullptr, ctx);
    if (!browser) {
        cerr << "Error: failed to create the browser window." << endl;
        XDestroyWindow(main_display, window_xid);
        XCloseDisplay(main_display);
        CefShutdown();
        return 1;
    }

    child_window = browser->GetHost()->GetWindowHandle();

    if (XClassHint *hint = XAllocClassHint()) {
        char name[] = "Adobe Connect";
        hint->res_class = name;
        hint->res_name = name;
        XSetClassHint(main_display, window_xid, hint);
        XFree(hint);  // the original strdup'd both names and leaked them
    }

    maximizeWindow(window_xid, main_display);

    while (isOpen)
    {
        pump_x_events();
        CefDoMessageLoopWork();
        this_thread::sleep_for(kFrameDelay);
    }

    // Shut down in order: browser first, then our window, then CEF.
    browser->GetHost()->CloseBrowser(true);
    // Give CEF a few frames to finish tearing the browser down before the
    // window it is parented to disappears.  CEF 86 exposes no "is it gone yet"
    // predicate, so this is a bounded wait rather than a poll.
    for (int i = 0; i < 30; ++i) {
        CefDoMessageLoopWork();
        this_thread::sleep_for(kFrameDelay);
    }
    browser = nullptr;

    XDestroySubwindows(main_display, window_xid);
    XDestroyWindow(main_display, window_xid);
    XCloseDisplay(main_display);
    main_display = nullptr;

    CefShutdown();
    return 0;
}
