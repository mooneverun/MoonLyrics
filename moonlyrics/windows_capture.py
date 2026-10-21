"""Sample a desktop region while excluding only our own view of the lyric window."""
import ctypes as C
from ctypes import wintypes as W
import threading
import time


def api(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


user32 = C.WinDLL("user32", use_last_error=True)
mag = C.WinDLL("Magnification", use_last_error=True)
client_rect = api(user32, "GetClientRect", W.BOOL, W.HWND, C.POINTER(W.RECT))
client_origin = api(user32, "ClientToScreen", W.BOOL, W.HWND, C.POINTER(W.POINT))
metrics = api(user32, "GetSystemMetrics", C.c_int, C.c_int)
create_window = api(user32, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                    C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p)
destroy_window = api(user32, "DestroyWindow", W.BOOL, W.HWND)
layer_alpha = api(user32, "SetLayeredWindowAttributes", W.BOOL, W.HWND, W.DWORD, W.BYTE, W.DWORD)
show_window = api(user32, "ShowWindow", W.BOOL, W.HWND, C.c_int)
position_window = api(user32, "SetWindowPos", W.BOOL, W.HWND, W.HWND,
                      C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
invalidate = api(user32, "InvalidateRect", W.BOOL, W.HWND, C.POINTER(W.RECT), W.BOOL)
update_window = api(user32, "UpdateWindow", W.BOOL, W.HWND)
peek_message = api(user32, "PeekMessageW", W.BOOL, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT)
translate_message = api(user32, "TranslateMessage", W.BOOL, C.POINTER(W.MSG))
dispatch_message = api(user32, "DispatchMessageW", C.c_ssize_t, C.POINTER(W.MSG))
mag_initialize = api(mag, "MagInitialize", W.BOOL)
mag_uninitialize = api(mag, "MagUninitialize", W.BOOL)
mag_source = api(mag, "MagSetWindowSource", W.BOOL, W.HWND, W.RECT)
mag_filter = api(mag, "MagSetWindowFilterList", W.BOOL, W.HWND, W.DWORD, C.c_int, C.POINTER(W.HWND))


class GUID(C.Structure):
    _fields_ = [("a", W.DWORD), ("b", W.WORD), ("c", W.WORD), ("d", C.c_ubyte * 8)]


class ImageHeader(C.Structure):
    _fields_ = [("width", W.UINT), ("height", W.UINT), ("format", GUID),
                ("stride", W.UINT), ("offset", W.UINT), ("size", C.c_size_t)]


ScalingCallback = C.WINFUNCTYPE(W.BOOL, W.HWND, C.c_void_p, ImageHeader,
                               C.c_void_p, ImageHeader, W.RECT, W.RECT, W.HRGN)
mag_callback = api(mag, "MagSetImageScalingCallback", W.BOOL, W.HWND, ScalingCallback)


def checked(result):
    if not result:
        raise C.WinError(C.get_last_error() or 5)
    return result


class _Sampler(threading.Thread):
    def __init__(self):
        super().__init__(name="MoonLyrics desktop sampling", daemon=True)
        self.lock = threading.Lock()
        self.wake, self.stopping = threading.Event(), threading.Event()
        self.pending = self.requested = self.latest = self.error = None
        self.serial = 0
        self.raw = None
        self.dimensions = (0, 0)
        self.callback = ScalingCallback(self.receive)

    def receive(self, hwnd, source, header, destination, target, unclipped, clipped, dirty):
        if (source and (header.width, header.height) == self.dimensions
                and header.width * 4 <= header.stride <= header.width * 4 + 4096
                and header.offset + header.stride * header.height <= header.size <= 300_000_000):
            self.raw = C.string_at(source + header.offset, header.stride * header.height), header.stride
        return True

    def run(self):
        host = child = None
        initialized = False
        try:
            checked(mag_initialize()); initialized = True
            # An almost invisible helper must render for the control to supply pixels.
            host = checked(create_window(0x080800A0, "Static", "", 0x80000000,
                                         0, 0, 1, 1, None, None, None, None))
            checked(layer_alpha(host, 0, 1, 2))
            child = checked(create_window(0, "Magnifier", "", 0x50000000,
                                          0, 0, 1, 1, host, None, None, None))
            # shortcut: this callback is deprecated; replace the backend if supported systems stop delivering frames.
            checked(mag_callback(child, self.callback))
            show_window(host, 8)
            checked(position_window(host, 1, 0, 0, 0, 0, 0x13))
            message = W.MSG()
            while not self.stopping.is_set():
                self.wake.wait(.1)
                with self.lock:
                    job, self.pending = self.pending, None
                    self.wake.clear()
                if self.stopping.is_set():
                    break
                if job is not None:
                    started = time.perf_counter()
                    hwnd, x, y, width, height = job
                    if self.dimensions != (width, height):
                        checked(position_window(host, 1, 0, 0, width, height, 0x14))
                        checked(position_window(child, None, 0, 0, width, height, 0x1C))
                        self.dimensions = width, height
                    checked(mag_filter(child, 0, 2, (W.HWND * 2)(hwnd, host)))
                    self.raw = None
                    checked(mag_source(child, W.RECT(x, y, x + width, y + height)))
                    # Setting the source often delivers the frame already; avoid a second DWM wait.
                    if self.raw is None:
                        checked(invalidate(child, None, False)); checked(update_window(child))
                    if self.raw is None:
                        raise OSError("桌面取样未返回画面")
                    with self.lock:
                        if job == self.requested:
                            self.serial += 1
                            self.latest = job, self.serial, self.raw, (time.perf_counter() - started) * 1000
                for _ in range(32):
                    if self.stopping.is_set() or not peek_message(C.byref(message), None, 0, 0, 1):
                        break
                    translate_message(C.byref(message)); dispatch_message(C.byref(message))
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
        finally:
            if host:
                destroy_window(host)
            if initialized:
                mag_uninitialize()
            self.callback = None
            self.raw = None


class DesktopRegion:
    """Keep a bounded latest-frame buffer; native sampling never blocks the Qt thread."""
    def __init__(self):
        self.worker = None
        self.buffer = None
        self.bits = C.c_void_p()
        self.size = (0, 0)
        self.bounds = (0., 0., 1., 1.)
        self.capture_ms = 0.
        self.serial = 0
        self.request_key = None
        self.wait_since = 0.
        self.last_request_at = 0.

    def reset(self):
        if not self.close():
            raise OSError("后台桌面取样尚未结束")

    def capture(self, hwnd, pad):
        rect, origin = W.RECT(), W.POINT()
        checked(client_rect(hwnd, C.byref(rect))); checked(client_origin(hwnd, C.byref(origin)))
        fw, fh = rect.right, rect.bottom
        width, height = fw + 2 * pad, fh + 2 * pad
        if min(fw, fh) < 1 or max(width, height) > 8192:
            raise RuntimeError("无效的桌面取样尺寸")
        x, y = origin.x - pad, origin.y - pad
        vx, vy, vw, vh = (metrics(n) for n in (76, 77, 78, 79))
        sx, sy = max(x, vx), max(y, vy)
        right, bottom = min(x + width, vx + vw), min(y + height, vy + vh)
        if right <= sx or bottom <= sy:
            raise RuntimeError("玻璃条位于显示器范围外")
        job = hwnd, sx, sy, right - sx, bottom - sy
        now = time.perf_counter()
        if self.worker is None:
            self.worker = _Sampler()
            self.worker.start()
            self.wait_since = now
        if job != self.request_key or now - self.last_request_at > 1:
            self.request_key = job
            self.wait_since = now
        self.last_request_at = now
        with self.worker.lock:
            if self.worker.error is not None:
                raise OSError(self.worker.error)
            latest = self.worker.latest
            self.worker.requested = self.worker.pending = job
            self.worker.wake.set()
        if latest is None or latest[0] != job or latest[1] == self.serial:
            if now - self.wait_since > 3:
                raise OSError("桌面取样未更新")
            return None
        self.serial = latest[1]
        self.wait_since = now
        if self.size != (width, height):
            self.buffer = C.create_string_buffer(width * height * 4)
            self.bits = C.cast(self.buffer, C.c_void_p)
            self.size = width, height
        raw, stride = latest[2]
        # Keep the renderer's existing bottom-up BGRA layout, including clipped monitor edges.
        for row in range(bottom - sy):
            offset = ((height - 1 - (sy - y + row)) * width + sx - x) * 4
            C.memmove(self.bits.value + offset, raw[row * stride:row * stride + (right - sx) * 4], (right - sx) * 4)
        self.bounds = ((sx - x + .5) / width, (height - (bottom - y) + .5) / height,
                       (right - x - .5) / width, (height - (sy - y) - .5) / height)
        self.capture_ms = latest[3]
        return fw, fh, pad

    def close(self):
        if self.worker is not None:
            self.worker.stopping.set(); self.worker.wake.set()
            self.worker.join(.3)
            if self.worker.is_alive():
                return False
            self.worker = None
        self.buffer = None
        self.bits = C.c_void_p()
        self.size = (0, 0)
        self.serial = 0
        self.request_key = None
        return True
