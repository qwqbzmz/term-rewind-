"""Keep one desktop app per interactive Windows session."""

import ctypes
from ctypes import wintypes


class AppInstance:
    def __init__(self, name=r"Local\term-rewind.desktop"):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateEventW.restype = wintypes.HANDLE
        self.api.SetEvent.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.event = self.api.CreateEventW(None, False, False, name + ".activate")
        if not self.event:
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.set_last_error(0)
        self.mutex = self.api.CreateMutexW(None, False, name + ".mutex")
        error = ctypes.get_last_error()
        if not self.mutex:
            self.api.CloseHandle(self.event)
            raise ctypes.WinError(error)
        self.primary = error != 183  # ERROR_ALREADY_EXISTS
        if not self.primary:
            self.api.SetEvent(self.event)

    def requested(self):
        return self.api.WaitForSingleObject(self.event, 0) == 0

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.api.CloseHandle(self.mutex)
        self.api.CloseHandle(self.event)
