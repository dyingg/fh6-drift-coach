"""Direct capture of the physical wheel (G29) as a passive HID observer.

The game's telemetry only knows the post-mapping steering value; reading
the device itself shows what the driver's HANDS did - wheel velocity,
whether they let it self-center through transitions, pedal overlap.
Recorded as a second stream (wheel.jsonl) alongside the telemetry.

Deliberately NOT SDL/pygame/DirectInput: SDL acquires force-feedback
devices in EXCLUSIVE background mode (it needs that for rumble), and
DirectInput exclusivity is single-owner - the game can no longer program
its own FFB, its wheel settings stop applying, and the Logitech driver
falls back to the default centering spring (the wheel suddenly feels
heavy). Instead this reads raw HID input reports through a SHARED-mode
handle: read-only, no acquisition, no output reports - physically unable
to touch force feedback, driver state, or the game's view of the device.
Pure ctypes, Windows only; without a wheel everything else still works.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time

RECORD_HZ = 100.0          # wheel.jsonl write cadence (reports can be faster)
WHEEL_KEYWORDS = ("G29", "G920", "G923", "G27", "G25", "LOGITECH", "WHEEL")

if sys.platform == "win32":
    from ctypes import wintypes

    _hid = ctypes.windll.hid
    _setupapi = ctypes.windll.setupapi
    _k32 = ctypes.windll.kernel32

    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                 wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p]
    _k32.CreateEventW.restype = ctypes.c_void_p
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                              wintypes.DWORD, ctypes.c_void_p,
                              ctypes.c_void_p]
    _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _k32.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(wintypes.DWORD),
                                         wintypes.BOOL]
    _k32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _k32.ResetEvent.argtypes = [ctypes.c_void_p]
    _setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    _setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p]
    _setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    for _fn in ("HidD_GetPreparsedData", "HidD_FreePreparsedData",
                "HidD_GetAttributes", "HidD_GetProductString"):
        getattr(_hid, _fn).argtypes = None  # default conversions, see calls

    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _HIDP_STATUS_SUCCESS = 0x00110000
    _ERROR_IO_PENDING = 997
    _WAIT_OBJECT_0, _WAIT_TIMEOUT = 0x0, 0x102

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", _GUID),
                    ("Flags", wintypes.DWORD),
                    ("Reserved", ctypes.POINTER(ctypes.c_ulong))]

    class _HIDD_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Size", ctypes.c_ulong), ("VendorID", ctypes.c_ushort),
                    ("ProductID", ctypes.c_ushort),
                    ("VersionNumber", ctypes.c_ushort)]

    class _HIDP_CAPS(ctypes.Structure):
        _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                    ("InputReportByteLength", ctypes.c_ushort),
                    ("OutputReportByteLength", ctypes.c_ushort),
                    ("FeatureReportByteLength", ctypes.c_ushort),
                    ("Reserved", ctypes.c_ushort * 17),
                    ("NumberLinkCollectionNodes", ctypes.c_ushort),
                    ("NumberInputButtonCaps", ctypes.c_ushort),
                    ("NumberInputValueCaps", ctypes.c_ushort),
                    ("NumberInputDataIndices", ctypes.c_ushort),
                    ("NumberOutputButtonCaps", ctypes.c_ushort),
                    ("NumberOutputValueCaps", ctypes.c_ushort),
                    ("NumberOutputDataIndices", ctypes.c_ushort),
                    ("NumberFeatureButtonCaps", ctypes.c_ushort),
                    ("NumberFeatureValueCaps", ctypes.c_ushort),
                    ("NumberFeatureDataIndices", ctypes.c_ushort)]

    class _RANGE(ctypes.Structure):
        _fields_ = [("UsageMin", ctypes.c_ushort), ("UsageMax", ctypes.c_ushort),
                    ("StringMin", ctypes.c_ushort), ("StringMax", ctypes.c_ushort),
                    ("DesignatorMin", ctypes.c_ushort),
                    ("DesignatorMax", ctypes.c_ushort),
                    ("DataIndexMin", ctypes.c_ushort),
                    ("DataIndexMax", ctypes.c_ushort)]

    class _NOTRANGE(ctypes.Structure):
        _fields_ = [("Usage", ctypes.c_ushort), ("Reserved1", ctypes.c_ushort),
                    ("StringIndex", ctypes.c_ushort),
                    ("Reserved2", ctypes.c_ushort),
                    ("DesignatorIndex", ctypes.c_ushort),
                    ("Reserved3", ctypes.c_ushort),
                    ("DataIndex", ctypes.c_ushort),
                    ("Reserved4", ctypes.c_ushort)]

    class _RANGE_UNION(ctypes.Union):
        _fields_ = [("Range", _RANGE), ("NotRange", _NOTRANGE)]

    class _HIDP_BUTTON_CAPS(ctypes.Structure):
        _fields_ = [("UsagePage", ctypes.c_ushort), ("ReportID", ctypes.c_ubyte),
                    ("IsAlias", ctypes.c_ubyte), ("BitField", ctypes.c_ushort),
                    ("LinkCollection", ctypes.c_ushort),
                    ("LinkUsage", ctypes.c_ushort),
                    ("LinkUsagePage", ctypes.c_ushort),
                    ("IsRange", ctypes.c_ubyte),
                    ("IsStringRange", ctypes.c_ubyte),
                    ("IsDesignatorRange", ctypes.c_ubyte),
                    ("IsAbsolute", ctypes.c_ubyte),
                    ("Reserved", ctypes.c_ulong * 10), ("u", _RANGE_UNION)]

    class _HIDP_VALUE_CAPS(ctypes.Structure):
        _fields_ = [("UsagePage", ctypes.c_ushort), ("ReportID", ctypes.c_ubyte),
                    ("IsAlias", ctypes.c_ubyte), ("BitField", ctypes.c_ushort),
                    ("LinkCollection", ctypes.c_ushort),
                    ("LinkUsage", ctypes.c_ushort),
                    ("LinkUsagePage", ctypes.c_ushort),
                    ("IsRange", ctypes.c_ubyte),
                    ("IsStringRange", ctypes.c_ubyte),
                    ("IsDesignatorRange", ctypes.c_ubyte),
                    ("IsAbsolute", ctypes.c_ubyte), ("HasNull", ctypes.c_ubyte),
                    ("Reserved", ctypes.c_ubyte), ("BitSize", ctypes.c_ushort),
                    ("ReportCount", ctypes.c_ushort),
                    ("Reserved2", ctypes.c_ushort * 5),
                    ("UnitsExp", ctypes.c_ulong), ("Units", ctypes.c_ulong),
                    ("LogicalMin", ctypes.c_long), ("LogicalMax", ctypes.c_long),
                    ("PhysicalMin", ctypes.c_long),
                    ("PhysicalMax", ctypes.c_long), ("u", _RANGE_UNION)]

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", ctypes.c_void_p)]

    def _hid_device_paths() -> list[str]:
        guid = _GUID()
        _hid.HidD_GetHidGuid(ctypes.byref(guid))
        devs = _setupapi.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
        if not devs or devs == _INVALID_HANDLE:
            return []
        paths, index = [], 0
        try:
            while True:
                ifd = _SP_DEVICE_INTERFACE_DATA()
                ifd.cbSize = ctypes.sizeof(ifd)
                if not _setupapi.SetupDiEnumDeviceInterfaces(
                        devs, None, ctypes.byref(guid), index,
                        ctypes.byref(ifd)):
                    break
                index += 1
                needed = wintypes.DWORD()
                _setupapi.SetupDiGetDeviceInterfaceDetailW(
                    devs, ctypes.byref(ifd), None, 0,
                    ctypes.byref(needed), None)
                buf = ctypes.create_string_buffer(needed.value)
                # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize (not buffer size)
                cb = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = cb
                if _setupapi.SetupDiGetDeviceInterfaceDetailW(
                        devs, ctypes.byref(ifd), buf, needed,
                        None, None):
                    paths.append(ctypes.wstring_at(
                        ctypes.addressof(buf) + 4))
        finally:
            _setupapi.SetupDiDestroyDeviceInfoList(devs)
        return paths

    class _HidWheel:
        """One opened HID joystick collection, shared read-only."""

        def __init__(self, path: str):
            self.ok = False
            self.handle = _k32.CreateFileW(
                path, 0x80000000,                       # GENERIC_READ only
                0x3, None, 3, 0x40000000, None)         # share RW, OVERLAPPED
            if not self.handle or self.handle == _INVALID_HANDLE:
                return
            self.preparsed = ctypes.c_void_p()
            if not _hid.HidD_GetPreparsedData(
                    ctypes.c_void_p(self.handle),
                    ctypes.byref(self.preparsed)):
                self.close()
                return
            caps = _HIDP_CAPS()
            if _hid.HidP_GetCaps(self.preparsed,
                                 ctypes.byref(caps)) != _HIDP_STATUS_SUCCESS:
                self.close()
                return
            # top-level collection must be a joystick/gamepad
            if caps.UsagePage != 0x01 or caps.Usage not in (0x04, 0x05):
                self.close()
                return
            self.report_len = caps.InputReportByteLength
            if self.report_len == 0:
                self.close()
                return

            attrs = _HIDD_ATTRIBUTES()
            attrs.Size = ctypes.sizeof(attrs)
            _hid.HidD_GetAttributes(ctypes.c_void_p(self.handle),
                                    ctypes.byref(attrs))
            self.vendor_id = attrs.VendorID
            namebuf = ctypes.create_unicode_buffer(126)
            _hid.HidD_GetProductString(ctypes.c_void_p(self.handle),
                                       namebuf, ctypes.sizeof(namebuf))
            self.name = namebuf.value or "HID wheel"

            # axes: every input value except the hat switch (0x39)
            self.axes_caps: list[tuple[int, int, int, int, int, int]] = []
            n = ctypes.c_ushort(caps.NumberInputValueCaps)
            if n.value:
                vcaps = (_HIDP_VALUE_CAPS * n.value)()
                if _hid.HidP_GetValueCaps(0, vcaps, ctypes.byref(n),
                                          self.preparsed) \
                        == _HIDP_STATUS_SUCCESS:
                    for c in vcaps[:n.value]:
                        # only real controls: Generic Desktop / Simulation
                        # pages, minus the hat switch (vendor pages carry
                        # rev-light/dial noise)
                        if c.UsagePage not in (0x01, 0x02):
                            continue
                        usages = (range(c.u.Range.UsageMin,
                                        c.u.Range.UsageMax + 1)
                                  if c.IsRange else [c.u.NotRange.Usage])
                        for usage in usages:
                            if c.UsagePage == 0x01 and usage == 0x39:
                                continue  # hat
                            self.axes_caps.append(
                                (c.UsagePage, usage, c.LinkCollection,
                                 c.LogicalMin, c.LogicalMax, c.BitSize))
            self.axes_caps.sort(key=lambda a: (a[0], a[1]))

            # buttons: highest usage on the Button page (0x09)
            self.num_buttons = 0
            n = ctypes.c_ushort(caps.NumberInputButtonCaps)
            if n.value:
                bcaps = (_HIDP_BUTTON_CAPS * n.value)()
                if _hid.HidP_GetButtonCaps(0, bcaps, ctypes.byref(n),
                                           self.preparsed) \
                        == _HIDP_STATUS_SUCCESS:
                    for c in bcaps[:n.value]:
                        if c.UsagePage != 0x09:
                            continue
                        top = (c.u.Range.UsageMax if c.IsRange
                               else c.u.NotRange.Usage)
                        self.num_buttons = max(self.num_buttons, top)
            self.max_usages = _hid.HidP_MaxUsageListLength(
                0, 0x09, self.preparsed) or self.num_buttons
            self.ok = True

        def parse(self, report, length: int,
                  axes: list[float], buttons: list[int]) -> None:
            """Decode one input report IN PLACE (a report that doesn't carry
            a usage keeps its previous value - multi-report devices)."""
            for i, (page, usage, link, lmin, lmax, bits) in \
                    enumerate(self.axes_caps):
                val = ctypes.c_ulong()
                if _hid.HidP_GetUsageValue(
                        0, page, link, usage, ctypes.byref(val),
                        self.preparsed, report, length) \
                        != _HIDP_STATUS_SUCCESS:
                    continue
                v = val.value
                if lmin < 0 and bits and v >= (1 << (bits - 1)):
                    v -= 1 << bits  # sign-extend
                if lmax > lmin:
                    axes[i] = round((v - lmin) / (lmax - lmin) * 2 - 1, 4)
            count = ctypes.c_ulong(self.max_usages)
            pressed = (ctypes.c_ushort * self.max_usages)()
            if _hid.HidP_GetUsages(0, 0x09, 0, pressed, ctypes.byref(count),
                                   self.preparsed, report, length) \
                    == _HIDP_STATUS_SUCCESS:
                down = {pressed[i] for i in range(count.value)}
                for b in range(self.num_buttons):
                    buttons[b] = 1 if (b + 1) in down else 0

        def close(self) -> None:
            pp = getattr(self, "preparsed", None)
            if pp:
                _hid.HidD_FreePreparsedData(pp)
                self.preparsed = None
            if self.handle and self.handle != _INVALID_HANDLE:
                _k32.CloseHandle(self.handle)
                self.handle = None

    def _open_wheel() -> _HidWheel | None:
        """Best joystick-type HID collection: prefer a Logitech wheel."""
        best: tuple[int, _HidWheel] | None = None
        for path in _hid_device_paths():
            try:
                dev = _HidWheel(path)
            except Exception:
                continue
            if not dev.ok:
                continue
            score = 0
            if any(k in dev.name.upper() for k in WHEEL_KEYWORDS):
                score += 2
            if dev.vendor_id == 0x046D:  # Logitech
                score += 1
            if best is None or score > best[0]:
                if best is not None:
                    best[1].close()
                best = (score, dev)
            else:
                dev.close()
        return best[1] if best else None


class WheelReader(threading.Thread):
    def __init__(self):
        super().__init__(name="wheel-reader", daemon=True)
        self._lock = threading.Lock()
        self._recorder = None
        self._running = True
        self.available = False
        self.device_name: str | None = None
        self._axes: list[float] = []
        self._buttons: list[int] = []
        self._presses: set[int] = set()  # down-edges since last consume
        self._dev = None

    def run(self) -> None:
        if sys.platform != "win32":
            return
        try:
            dev = _open_wheel()
        except Exception:
            return
        if dev is None:
            return
        self._dev = dev
        self.device_name = dev.name
        self.available = True

        axes = [0.0] * len(dev.axes_caps)
        buttons = [0] * dev.num_buttons
        # The wheel only SENDS a report when its state changes, so seed the
        # current state synchronously (harmless if the driver declines).
        try:
            seed = (ctypes.c_ubyte * dev.report_len)()
            if _hid.HidD_GetInputReport(ctypes.c_void_p(dev.handle),
                                        seed, dev.report_len):
                dev.parse(seed, dev.report_len, axes, buttons)
        except Exception:
            pass
        prev = list(buttons)
        with self._lock:
            self._axes, self._buttons = list(axes), list(buttons)
        buf = (ctypes.c_ubyte * dev.report_len)()
        ov = _OVERLAPPED()
        ov.hEvent = _k32.CreateEventW(None, True, False, None)
        last_rec = 0.0
        try:
            while self._running:
                _k32.ResetEvent(ov.hEvent)
                ok = _k32.ReadFile(dev.handle, buf, dev.report_len,
                                   None, ctypes.byref(ov))
                if not ok and _k32.GetLastError() != _ERROR_IO_PENDING:
                    break  # device unplugged
                while self._running:
                    w = _k32.WaitForSingleObject(ov.hEvent, 200)
                    if w != _WAIT_TIMEOUT:
                        break
                if not self._running:
                    _k32.CancelIoEx(dev.handle, ctypes.byref(ov))
                    break
                done = wintypes.DWORD()
                if not _k32.GetOverlappedResult(dev.handle, ctypes.byref(ov),
                                                ctypes.byref(done), False):
                    break
                dev.parse(buf, dev.report_len, axes, buttons)
                ts = time.time()
                edges = {i for i, b in enumerate(buttons)
                         if b and (i >= len(prev) or not prev[i])}
                prev = list(buttons)
                with self._lock:
                    self._axes, self._buttons = list(axes), list(buttons)
                    self._presses |= edges
                    recorder = self._recorder
                if recorder is not None and ts - last_rec >= 1.0 / RECORD_HZ:
                    recorder.write_wheel(ts, list(axes), list(buttons))
                    last_rec = ts
        finally:
            if ov.hEvent:
                _k32.CloseHandle(ov.hEvent)
            dev.close()

    def set_recorder(self, recorder) -> None:
        """Called by the listener when recording starts (Recorder) / stops (None)."""
        with self._lock:
            self._recorder = recorder

    def snapshot(self) -> tuple[str | None, list[float]]:
        with self._lock:
            return self.device_name, list(self._axes)

    def consume_presses(self) -> set[int]:
        """Button indices that had a press (down-edge) since the last call.
        Polled by the UI tick - keeps tkinter interaction on the UI thread."""
        with self._lock:
            presses, self._presses = self._presses, set()
        return presses

    def stop(self) -> None:
        self._running = False
