/* Windows Media Foundation player for a single, seekable Digger preview. */
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mfplay.h>
#include <shellapi.h>

static IMFPMediaPlayer *player;

static LRESULT CALLBACK events(HWND window, UINT message, WPARAM key, LPARAM value)
{
    if (message == WM_KEYDOWN && key == VK_ESCAPE) {
        DestroyWindow(window);
        return 0;
    }
    if (player && ((message == WM_KEYDOWN && key == VK_SPACE) || message == WM_COMMAND)) {
        MFP_MEDIAPLAYER_STATE state;
        if (SUCCEEDED(IMFPMediaPlayer_GetState(player, &state))) {
            if (state == MFP_MEDIAPLAYER_STATE_PLAYING) IMFPMediaPlayer_Pause(player);
            else IMFPMediaPlayer_Play(player);
        }
        return 0;
    }
    if (player && (message == WM_SIZE || message == WM_PAINT)) IMFPMediaPlayer_UpdateVideo(player);
    if (message == WM_DESTROY) {
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, key, value);
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command, int show)
{
    int count;
    LPWSTR *args = CommandLineToArgvW(GetCommandLineW(), &count);
    if (!args || count != 3 || wcsncmp(args[1], L"http://127.0.0.1:", 17)) return 1;
    if (FAILED(CoInitializeEx(NULL, COINIT_APARTMENTTHREADED))) return 1;
    WNDCLASSW cls = {0};
    cls.lpfnWndProc = events;
    cls.hInstance = instance;
    cls.lpszClassName = L"DiggerVideoPreview";
    cls.hCursor = LoadCursor(NULL, IDC_ARROW);
    cls.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
    RegisterClassW(&cls);
    HMENU menu = CreateMenu();
    AppendMenuW(menu, MF_STRING, 1, L"Play / Pause   (Space)");
    HWND window = CreateWindowW(cls.lpszClassName, args[2], WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT, CW_USEDEFAULT, 850, 540, NULL, menu, instance, NULL);
    int result = 1;
    if (window && SUCCEEDED(MFPCreateMediaPlayer(args[1], TRUE, 0, NULL, window, &player))) {
        ShowWindow(window, show);
        MSG message;
        while (GetMessageW(&message, NULL, 0, 0) > 0) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        IMFPMediaPlayer_Shutdown(player);
        IMFPMediaPlayer_Release(player);
        result = 0;
    }
    LocalFree(args);
    CoUninitialize();
    return result;
}
