"""Windows 原生「选择文件夹 / 选择文件」对话框。

不依赖 tkinter——打包时 build.bat 刻意排除了 tkinter（省体积），之前用
tkinter 弹框在 exe 里会报 No module named 'tkinter'。

实现选经典 Win32 API（直接调 DLL 导出，纯结构体、无 COM vtable 手搓）：
- 选文件：comdlg32.GetOpenFileNameW（Win10/11 上自动呈现新式对话框）
- 选文件夹：shell32.SHBrowseForFolderW（BIF_NEWDIALOGSTYLE 新式树 + 地址框）

非 Windows 或调用异常时回退 tkinter；两者都不可用则抛 RuntimeError，
由调用方提示用户手动输入路径。
"""
import os
import sys


def pick_folder(title: str, initial: str = "") -> str:
    """弹「选择文件夹」，返回所选目录（正斜杠形式）；取消返回空串。"""
    return _pick(True, title, initial, None)


def pick_file(title: str, initial: str = "", filetypes=None) -> str:
    """弹「选择文件」，filetypes 为 tkinter 风格 [(标签, '*.mp3 *.m4a'), ...]。"""
    return _pick(False, title, initial, filetypes)


def _pick(is_folder: bool, title: str, initial: str, filetypes) -> str:
    if sys.platform == "win32":
        try:
            if is_folder:
                return _win_pick_folder(title, initial)
            return _win_pick_file(title, initial, filetypes)
        except OSError:
            raise
    return _pick_tk(is_folder, title, initial, filetypes)


# ---------------------------------------------------------------- tkinter 兜底

def _pick_tk(is_folder: bool, title: str, initial: str, filetypes) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    init = initial if initial and os.path.exists(initial) else os.path.expanduser("~")
    if is_folder:
        path = filedialog.askdirectory(initialdir=init, title=title, mustexist=False)
    else:
        path = filedialog.askopenfilename(initialdir=init, title=title,
                                          filetypes=filetypes or [("所有文件", "*.*")])
    root.destroy()
    return (path or "").replace("\\", "/")


# ------------------------------------------------------------- Windows 原生通道

def _topmost_owner():
    """造一个 0 尺寸的置顶影子窗口当对话框属主：用户在浏览器里点按钮时，
    对话框不会被压到浏览器后面。用完记得 DestroyWindow。"""
    import ctypes
    from ctypes import c_int, c_uint, c_void_p, c_wchar_p, windll

    user32 = windll.user32
    WS_POPUP = 0x80000000
    WS_EX_TOPMOST = 0x8
    HWND_TOPMOST = -1
    SWP_NOMOVE, SWP_NOSIZE = 0x2, 0x1

    user32.CreateWindowExW.argtypes = [c_uint, c_wchar_p, c_wchar_p, c_uint,
                                       c_int, c_int, c_int, c_int,
                                       c_void_p, c_void_p, c_void_p, c_void_p]
    user32.CreateWindowExW.restype = c_void_p
    user32.SetWindowPos.argtypes = [c_void_p, c_void_p, c_int, c_int, c_int, c_int, c_uint]
    hwnd = user32.CreateWindowExW(WS_EX_TOPMOST, "STATIC", "钩子工坊", WS_POPUP,
                                  0, 0, 0, 0, None, None, None, None)
    user32.SetWindowPos(hwnd, c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE)
    return hwnd


def _win_pick_folder(title: str, initial: str) -> str:
    import ctypes
    from ctypes import POINTER, byref, c_void_p, c_wchar_p, windll

    user32, shell32, ole32 = windll.user32, windll.shell32, windll.ole32
    MAX_PATH = 260
    BIF_RETURNONLYFSDIRS = 0x1
    BIF_EDITBOX = 0x10
    BIF_NEWDIALOGSTYLE = 0x40
    BFFM_INITIALIZED = 1
    BFFM_SETSELECTIONW = 0x400 + 102  # WM_USER + 102

    ole32.CoInitializeEx(None, 0x2)
    try:
        display = ctypes.create_unicode_buffer(MAX_PATH)

        # 回调：对话框初始化完成时，把初始路径发进去
        init_path = ctypes.c_wchar_p(os.path.normpath(initial)) \
            if initial and os.path.isdir(initial) else None

        def proc(hwnd, msg, lp, _data):
            if msg == BFFM_INITIALIZED and init_path:
                user32.SendMessageW(hwnd, BFFM_SETSELECTIONW, 1,
                                    ctypes.cast(init_path, c_void_p).value)
            return 0

        PROCTYPE = ctypes.WINFUNCTYPE(ctypes.c_int, c_void_p,
                                      ctypes.c_uint, ctypes.c_ssize_t, ctypes.c_ssize_t)
        cb = PROCTYPE(proc)  # 保持在作用域内防 GC

        class BROWSEINFOW(ctypes.Structure):
            _fields_ = [("hwndOwner", c_void_p), ("pidlRoot", c_void_p),
                        ("pszDisplayName", c_wchar_p), ("lpszTitle", c_wchar_p),
                        ("ulFlags", ctypes.c_uint), ("lpfn", PROCTYPE),
                        ("lParam", ctypes.c_ssize_t), ("iImage", ctypes.c_int)]

        # x64 关键：显式声明原型，否则 Python int 指针被截断成 32 位
        user32.SendMessageW.argtypes = [c_void_p, ctypes.c_uint,
                                        ctypes.c_ssize_t, ctypes.c_ssize_t]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.DestroyWindow.argtypes = [c_void_p]
        shell32.SHBrowseForFolderW.argtypes = [POINTER(BROWSEINFOW)]
        shell32.SHBrowseForFolderW.restype = c_void_p
        shell32.SHGetPathFromIDListW.argtypes = [c_void_p, c_wchar_p]
        shell32.SHGetPathFromIDListW.restype = ctypes.c_int
        ole32.CoTaskMemFree.argtypes = [c_void_p]

        owner = _topmost_owner()
        try:
            bi = BROWSEINFOW()
            bi.hwndOwner = owner
            bi.pszDisplayName = ctypes.cast(display, c_wchar_p)
            bi.lpszTitle = title
            bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE
            bi.lpfn = cb
            pidl = shell32.SHBrowseForFolderW(byref(bi))
            if not pidl:
                return ""
            try:
                path = ctypes.create_unicode_buffer(MAX_PATH)
                if not shell32.SHGetPathFromIDListW(pidl, path):
                    return ""
                return path.value.replace("\\", "/")
            finally:
                ole32.CoTaskMemFree(pidl)
        finally:
            user32.DestroyWindow(owner)
    finally:
        ole32.CoUninitialize()


def _win_pick_file(title: str, initial: str, filetypes) -> str:
    import ctypes
    from ctypes import byref, c_wchar_p, c_void_p, windll

    comdlg32, ole32 = windll.comdlg32, windll.ole32
    OFN_HIDEREADONLY = 0x4
    OFN_PATHMUSTEXIST = 0x800
    OFN_FILEMUSTEXIST = 0x1000

    ole32.CoInitializeEx(None, 0x2)
    try:
        # 过滤器："音频文件\0*.mp3;*.m4a\0所有文件\0*.*\0\0"
        parts = []
        for label, pats in (filetypes or [("所有文件", "*.*")]):
            spec = ";".join(p.strip() for p in str(pats).split() if p.strip())
            if spec:
                parts.append(str(label))
                parts.append(spec)
        filter_str = "\0".join(parts) + "\0\0" if parts else "所有文件\0*.*\0\0"

        buf = ctypes.create_unicode_buffer(4096)
        buf.value = ""  # 选中后回填完整路径
        if initial and os.path.isfile(initial):
            # 传入的是文件：预填文件名、定位到其目录（重选 BGM 时体验更好）
            d, n = os.path.split(os.path.normpath(initial))
            initial = d
            buf.value = n

        class OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize", ctypes.c_ulong), ("hwndOwner", c_void_p),
                ("hInstance", c_void_p), ("lpstrFilter", c_wchar_p),
                ("lpstrCustomFilter", c_wchar_p), ("nMaxCustFilter", ctypes.c_ulong),
                ("nFilterIndex", ctypes.c_ulong), ("lpstrFile", c_wchar_p),
                ("nMaxFile", ctypes.c_ulong), ("lpstrFileTitle", c_wchar_p),
                ("nMaxFileTitle", ctypes.c_ulong), ("lpstrInitialDir", c_wchar_p),
                ("lpstrTitle", c_wchar_p), ("Flags", ctypes.c_ulong),
                ("nFileOffset", ctypes.c_ushort), ("nFileExtension", ctypes.c_ushort),
                ("lpstrDefExt", c_wchar_p), ("lCustData", ctypes.c_ssize_t),
                ("lpfnHook", c_void_p), ("lpTemplateName", c_void_p),
                ("pvReserved", c_void_p), ("dwReserved", ctypes.c_ulong),
                ("FlagsEx", ctypes.c_ulong),
            ]

        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        ofn.lpstrFilter = filter_str
        ofn.lpstrFile = ctypes.cast(buf, c_wchar_p)
        ofn.nMaxFile = 4096
        if initial and os.path.isdir(initial):
            ofn.lpstrInitialDir = os.path.normpath(initial)
        ofn.lpstrTitle = title
        ofn.Flags = OFN_HIDEREADONLY | OFN_PATHMUSTEXIST | OFN_FILEMUSTEXIST

        user32 = windll.user32
        user32.DestroyWindow.argtypes = [c_void_p]
        owner = _topmost_owner()
        try:
            ofn.hwndOwner = owner
            if not comdlg32.GetOpenFileNameW(byref(ofn)):
                # 取消时 CommDlgExtendedError() 返回 0；非 0 才是出错
                if comdlg32.CommDlgExtendedError() != 0:
                    raise OSError(
                        f"GetOpenFileName 失败 err={comdlg32.CommDlgExtendedError()}")
                return ""
            return buf.value.replace("\\", "/")
        finally:
            user32.DestroyWindow(owner)
    finally:
        ole32.CoUninitialize()


# ---------------------------------------------------------- 多选（批量发布用）

def pick_files(title: str, initial: str = "", filetypes=None) -> list[str]:
    """弹「选择文件」并允许多选，返回绝对路径列表（取消返回 []）。

    为什么不复用 pick_file：那个用的是单选的 GetOpenFileName 标志位，
    也只有一个路径的回填缓冲；多选时系统会把「目录 + 多个文件名」用 \\0
    分隔写进缓冲，解析方式完全不同。两件事混在一个函数里会把老调用点改坏，
    所以这里独立一份（2026-11-13 批量发布）。
    """
    if sys.platform == "win32":
        return _win_pick_files(title, initial, filetypes)
    return _pick_tk_files(title, initial, filetypes)


def _pick_tk_files(title: str, initial: str, filetypes) -> list[str]:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    init = initial if initial and os.path.exists(initial) else os.path.expanduser("~")
    paths = filedialog.askopenfilenames(initialdir=init, title=title,
                                        filetypes=filetypes or [("所有文件", "*.*")])
    root.destroy()
    return [p.replace("\\", "/") for p in (paths or [])]


def _win_pick_files(title: str, initial: str, filetypes) -> list[str]:
    import ctypes
    from ctypes import byref, c_wchar_p, c_void_p, windll

    comdlg32, ole32 = windll.comdlg32, windll.ole32
    OFN_HIDEREADONLY = 0x4
    OFN_PATHMUSTEXIST = 0x800
    OFN_FILEMUSTEXIST = 0x1000
    OFN_ALLOWMULTISELECT = 0x200
    OFN_EXPLORER = 0x80000

    ole32.CoInitializeEx(None, 0x2)
    try:
        parts = []
        for label, pats in (filetypes or [("所有文件", "*.*")]):
            spec = ";".join(p.strip() for p in str(pats).split() if p.strip())
            if spec:
                parts.append(str(label))
                parts.append(spec)
        filter_str = "\0".join(parts) + "\0\0" if parts else "所有文件\0*.*\0\0"

        # 多选时缓冲要放得下「目录 + 每个文件名」：64K 字符够几百个文件
        size = 65536
        buf = ctypes.create_unicode_buffer(size)

        class OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize", ctypes.c_ulong), ("hwndOwner", c_void_p),
                ("hInstance", c_void_p), ("lpstrFilter", c_wchar_p),
                ("lpstrCustomFilter", c_wchar_p), ("nMaxCustFilter", ctypes.c_ulong),
                ("nFilterIndex", ctypes.c_ulong), ("lpstrFile", c_wchar_p),
                ("nMaxFile", ctypes.c_ulong), ("lpstrFileTitle", c_wchar_p),
                ("nMaxFileTitle", ctypes.c_ulong), ("lpstrInitialDir", c_wchar_p),
                ("lpstrTitle", c_wchar_p), ("Flags", ctypes.c_ulong),
                ("nFileOffset", ctypes.c_ushort), ("nFileExtension", ctypes.c_ushort),
                ("lpstrDefExt", c_wchar_p), ("lCustData", ctypes.c_ssize_t),
                ("lpfnHook", c_void_p), ("lpTemplateName", c_void_p),
                ("pvReserved", c_void_p), ("dwReserved", ctypes.c_ulong),
                ("FlagsEx", ctypes.c_ulong),
            ]

        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        ofn.lpstrFilter = filter_str
        ofn.lpstrFile = ctypes.cast(buf, c_wchar_p)
        ofn.nMaxFile = size
        if initial and os.path.isdir(initial):
            ofn.lpstrInitialDir = os.path.normpath(initial)
        ofn.lpstrTitle = title
        ofn.Flags = (OFN_HIDEREADONLY | OFN_PATHMUSTEXIST | OFN_FILEMUSTEXIST
                     | OFN_ALLOWMULTISELECT | OFN_EXPLORER)

        user32 = windll.user32
        user32.DestroyWindow.argtypes = [c_void_p]
        owner = _topmost_owner()
        try:
            ofn.hwndOwner = owner
            if not comdlg32.GetOpenFileNameW(byref(ofn)):
                if comdlg32.CommDlgExtendedError() != 0:
                    raise OSError(
                        f"GetOpenFileName 失败 err={comdlg32.CommDlgExtendedError()}")
                return []
            raw = buf[:size]
        finally:
            user32.DestroyWindow(owner)
    finally:
        ole32.CoUninitialize()

    # 单选时 raw 就是完整路径；多选时是 目录\0名1\0名2\0\0
    segs = [s for s in raw.split("\0") if s]
    if not segs:
        return []
    if len(segs) == 1:
        return [segs[0].replace("\\", "/")]
    base = segs[0]
    if not os.path.isdir(base):
        return [base.replace("\\", "/")]
    return [os.path.join(base, n).replace("\\", "/") for n in segs[1:]]
