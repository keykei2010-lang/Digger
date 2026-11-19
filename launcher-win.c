#define UNICODE
#define _UNICODE
#include <windows.h>
#include <wchar.h>

int WINAPI WinMain(HINSTANCE instance, HINSTANCE previous, LPSTR args, int show)
{
    wchar_t app[MAX_PATH];
    wchar_t python[MAX_PATH];
    wchar_t desktop[MAX_PATH];
    wchar_t command[MAX_PATH * 2 + 8];
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    DWORD capacity = sizeof(app) / sizeof(*app);
    DWORD length = GetModuleFileNameW(NULL, app, capacity);

    if (length > 0 && length < capacity) {
        wchar_t *last_slash = wcsrchr(app, L'\\');
        if (last_slash != NULL) {
            *last_slash = L'\0';
            if (swprintf(python, MAX_PATH, L"%ls\\runtime\\pythonw.exe", app) > 0 &&
                swprintf(desktop, MAX_PATH, L"%ls\\desktop.py", app) > 0 &&
                swprintf(command, MAX_PATH * 2 + 8, L"\"%ls\" \"%ls\"", python, desktop) > 0) {
                startup.cb = sizeof(startup);
                if (CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, app, &startup, &process)) {
                    CloseHandle(process.hThread);
                    CloseHandle(process.hProcess);
                    return 0;
                }
            }
        }
    }

    MessageBoxW(NULL, L"Extract the entire Digger ZIP to a local folder, then run Digger.exe.",
                L"Digger", MB_OK | MB_ICONERROR);
    return 1;
}
