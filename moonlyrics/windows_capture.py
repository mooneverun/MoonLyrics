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
        self.pending = self.latest = self.error = None
        self.clients = set()
        self.client_pending, self.client_latest = {}, {}
        self.prefer_client = False
        self.excluded_windows = ()
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
        filtered_windows = None
        source_rect = None
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
                    client = None
                    if self.client_pending and (self.pending is None or self.prefer_client):
                        client = next(iter(self.client_pending))
                        job = self.client_pending.pop(client)
                        self.prefer_client = False
                    else:
                        job, self.pending = self.pending, None
                        self.prefer_client = True
                    self.wake.clear()
                    if self.pending is not None or self.client_pending:
                        self.wake.set()
                    exclusions = self.excluded_windows
                if self.stopping.is_set():
                    break
                if job is not None:
                    started = time.perf_counter()
                    hwnd, x, y, width, height = job
                    resized = self.dimensions != (width, height)
                    source_changed = source_rect != (x,y,width,height)
                    windows = tuple(dict.fromkeys((*exclusions,hwnd,host)))
                    filter_changed = windows != filtered_windows
                    if resized:
                        checked(position_window(host, 1, 0, 0, width, height, 0x14))
                        checked(position_window(child, None, 0, 0, width, height, 0x1C))
                        self.dimensions = width, height
                    if filter_changed:
                        checked(mag_filter(child,0,len(windows),(W.HWND*len(windows))(*windows)))
                        filtered_windows = windows
                    self.raw = None
                    checked(mag_source(child, W.RECT(x, y, x + width, y + height)))
                    source_rect = x,y,width,height
                    # A geometry/filter change can first deliver a provisional
                    # black or old-position surface. Finish its redraw before publishing it.
                    # The unchanged single-window path still avoids that wait.
                    if source_changed or filter_changed or self.raw is None:
                        checked(invalidate(child, None, False)); checked(update_window(child))
                    if self.raw is None:
                        raise OSError("桌面取样未返回画面")
                    with self.lock:
                        self.serial += 1
                        frame = job, self.serial, self.raw, (time.perf_counter() - started) * 1000
                        if client is None:
                            self.latest = frame
                        elif client in self.clients:
                            self.client_latest[client] = frame
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


class _SamplerClient:
    """One bounded mailbox on the lyric sampler, without a second native callback."""
    def __init__(self, worker):
        self.source = worker
        self.key = object()
        self.lock, self.wake = worker.lock, worker.wake
        with self.lock:
            worker.clients.add(self.key)

    @property
    def latest(self):
        return self.source.client_latest.get(self.key)

    @property
    def error(self):
        return self.source.error

    @property
    def pending(self):
        return self.source.client_pending.get(self.key)

    @pending.setter
    def pending(self, value):
        self.source.client_pending[self.key] = value

    @property
    def excluded_windows(self):
        return self.source.excluded_windows

    @excluded_windows.setter
    def excluded_windows(self, value):
        self.source.excluded_windows = value

    def close(self):
        with self.lock:
            self.source.clients.discard(self.key)
            self.source.client_pending.pop(self.key, None)
            self.source.client_latest.pop(self.key, None)


class DesktopRegion:
    """Keep a bounded latest-frame buffer; native sampling never blocks the Qt thread."""
    def __init__(self, *, shared_with=None):
        self.shared_with = shared_with
        self.excluded_windows = ()
        self.worker = None
        self.buffer = None
        self.top_down = True
        self.bits = C.c_void_p()
        self.size = (0, 0)
        self.bounds = (0., 0., 1., 1.)
        self.capture_ms = 0.
        self.serial = 0
        self.revision = 0
        self.source_rect = None
        self.offset = (0, 0)
        self.request_key = None
        self.request_shape = None
        self.frame_key = None
        self.wait_since = 0.
        self.last_request_at = 0.

    def reset(self):
        if not self.close():
            raise OSError("后台桌面取样尚未结束")

    def capture(self, hwnd, pad, margin=0, refresh=True):
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
        left, top = max(vx, sx-margin), max(vy, sy-margin)
        job = hwnd, left, top, min(vx+vw, right+margin)-left, min(vy+vh, bottom+margin)-top
        if max(job[3:]) > 8192:
            raise RuntimeError("无效的桌面取样尺寸")
        now = time.perf_counter()
        shape = fw, fh, pad, margin, vx, vy, vw, vh
        guard = min(64, margin//4)
        previous = self.request_key
        anchored = (previous is not None and previous[0] == hwnd and self.request_shape == shape
                    and previous[1] <= max(vx, sx-guard) and previous[2] <= max(vy, sy-guard)
                    and previous[1]+previous[3] >= min(vx+vw, right+guard)
                    and previous[2]+previous[4] >= min(vy+vh, bottom+guard))
        if anchored:
            job = previous
        if self.shared_with is not None:
            primary = self.shared_with
            primary.ensure_shared_worker()
            if self.worker is None or self.worker.source is not primary.worker:
                if self.worker is not None:
                    self.worker.close()
                self.worker = _SamplerClient(primary.worker)
                self.serial = 0
                self.source_rect = self.frame_key = self.request_key = None
                self.wait_since = now
        elif self.worker is None:
            self.worker = _Sampler()
            self.worker.start()
            self.wait_since = now
        changed = job != self.request_key or self.request_shape != shape
        if changed or now - self.last_request_at > 1:
            self.request_key = job
            self.request_shape = shape
            self.wait_since = now
        self.last_request_at = now
        with self.worker.lock:
            self.worker.excluded_windows = (self.shared_with.excluded_windows
                                            if self.shared_with is not None else self.excluded_windows)
            if self.worker.error is not None:
                raise OSError(self.worker.error)
            latest = self.worker.latest
            if refresh or changed or latest is None:
                self.worker.pending = job
                self.worker.wake.set()
        frame_key = (latest[1], x, y, width, height) if latest else None
        covers = (latest is not None and latest[0][0] == hwnd
                  and latest[0][1] <= sx and latest[0][2] <= sy
                  and latest[0][1]+latest[0][3] >= right and latest[0][2]+latest[0][4] >= bottom)
        if not covers or frame_key == self.frame_key:
            if now - self.wait_since > 3:
                raise OSError("桌面取样未更新")
            return None
        self.frame_key = frame_key
        self.wait_since = now
        if latest[1] != self.serial or latest[0] != self.source_rect:
            previous_source = self.source_rect
            self.serial = latest[1]
            self.source_rect = latest[0]
            cw, ch = latest[0][3:]
            raw, stride = latest[2]
            # Retain the native immutable owner; the GPU's first blur pass
            # handles orientation instead of allocating/copying every row.
            pixels = raw if stride == cw*4 else b''.join(raw[row*stride:row*stride+cw*4] for row in range(ch))
            changed = previous_source != self.source_rect or self.buffer != pixels
            self.buffer = pixels
            self.bits = C.cast(C.c_char_p(pixels), C.c_void_p)
            self.size = cw, ch
            self.bounds = (.5/cw, .5/ch, 1-.5/cw, 1-.5/ch)
            if changed:
                self.revision += 1
        source_x, source_y, cw, ch = self.source_rect[1:]
        self.offset = origin.x-source_x, source_y+ch-origin.y-fh
        self.capture_ms = latest[3]
        return fw, fh, pad

    def ensure_shared_worker(self):
        worker = self.worker
        if worker is not None and (worker.error is not None or worker.stopping.is_set()):
            worker.stopping.set(); worker.wake.set(); worker.join(.3)
            if worker.is_alive():
                raise OSError("后台桌面取样尚未结束")
            self.worker = None
            # Keep published pixels alive until the next GPU upload.
            self.serial = 0
            self.source_rect = self.frame_key = self.request_key = None
        if self.worker is None:
            self.worker = _Sampler()
            self.worker.start()
            self.wait_since = time.perf_counter()

    def close(self):
        if self.shared_with is not None and self.worker is not None:
            self.worker.close()
            self.worker = None
        elif self.worker is not None:
            self.worker.stopping.set(); self.worker.wake.set()
            self.worker.join(.3)
            if self.worker.is_alive():
                return False
            self.worker = None
        self.buffer = None
        self.bits = C.c_void_p()
        self.size = (0, 0)
        self.serial = 0
        self.source_rect = None
        self.offset = (0, 0)
        self.request_key = None
        self.request_shape = None
        self.frame_key = None
        return True
