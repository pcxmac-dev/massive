"""What the app's processes hold in memory, apart from Windows' file cache (`admin memory`).

A process's own memory is its private bytes (what it allocated: caches, frames, buffers) and its
working set (the part of it, and of the files it maps, in RAM now). Windows' file cache is the
system's, not any process's: pages of files read recently, kept in RAM while nothing else needs
it. Active (the system's working set: RAMMap's "Active" for a file) is held; standby is handed
back the moment anything asks, and counts as available. The chart app's live check once read a
symbol's every trade every few seconds, and 31 GB of the tick file stayed active in the cache
while the app itself held 1 GB: the two are measured apart. Disk reads per process too (all
since it started, and over a few seconds), to see which process is reading.

Windows only (the system's own counters through ctypes, no administrator rights needed);
elsewhere every figure is None."""

from __future__ import annotations

import ctypes
import os
import time

APP = "massive_collate"
MB = 2**20


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _process_counters(pid: int) -> dict | None:
    """{private, working_set, read} bytes of one process (read: from disk since it started)."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    size_t = ctypes.c_size_t

    class Memory(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", size_t),
                    ("WorkingSetSize", size_t), ("QuotaPeakPagedPoolUsage", size_t), ("QuotaPagedPoolUsage", size_t),
                    ("QuotaPeakNonPagedPoolUsage", size_t), ("QuotaNonPagedPoolUsage", size_t),
                    ("PagefileUsage", size_t), ("PeakPagefileUsage", size_t), ("PrivateUsage", size_t)]

    class Io(ctypes.Structure):  # IO_COUNTERS
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                        "OtherOperationCount", "ReadTransferCount",
                                                        "WriteTransferCount", "OtherTransferCount")]

    k32 = _kernel32()
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        mem, io = Memory(), Io()
        mem.cb = ctypes.sizeof(mem)
        if not k32.K32GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb):
            return None
        read = io.ReadTransferCount if k32.GetProcessIoCounters(handle, ctypes.byref(io)) else None
        return {"private": mem.PrivateUsage, "working_set": mem.WorkingSetSize, "read": read}
    finally:
        k32.CloseHandle(handle)


def _system() -> dict | None:
    """{total, available, commit, commit_limit, cache (standby and active together)} bytes."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    size_t = ctypes.c_size_t

    class Performance(ctypes.Structure):  # PERFORMANCE_INFORMATION
        _fields_ = [("cb", wintypes.DWORD), ("CommitTotal", size_t), ("CommitLimit", size_t),
                    ("CommitPeak", size_t), ("PhysicalTotal", size_t), ("PhysicalAvailable", size_t),
                    ("SystemCache", size_t), ("KernelTotal", size_t), ("KernelPaged", size_t),
                    ("KernelNonpaged", size_t), ("PageSize", size_t), ("HandleCount", wintypes.DWORD),
                    ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]

    perf = Performance()
    perf.cb = ctypes.sizeof(perf)
    if not _kernel32().K32GetPerformanceInfo(ctypes.byref(perf), perf.cb):
        return None
    page = perf.PageSize
    return {"total": perf.PhysicalTotal * page, "available": perf.PhysicalAvailable * page,
            "commit": perf.CommitTotal * page, "commit_limit": perf.CommitLimit * page,
            "cache": perf.SystemCache * page}  # SystemCache: the standby list and the system working set


def _active_cache() -> dict | None:
    """{active, peak} bytes of the file cache's working set (the system's: held, not standby)."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    size_t = ctypes.c_size_t

    class FileCache(ctypes.Structure):  # SYSTEM_FILECACHE_INFORMATION
        _fields_ = [("CurrentSize", size_t), ("PeakSize", size_t), ("PageFaultCount", wintypes.ULONG),
                    ("MinimumWorkingSet", size_t), ("MaximumWorkingSet", size_t),
                    ("CurrentSizeIncludingTransitionInPages", size_t), ("PeakSizeIncludingTransitionInPages", size_t),
                    ("TransitionRePurposeCount", wintypes.ULONG), ("Flags", wintypes.ULONG)]

    info, got = FileCache(), wintypes.ULONG()
    status = ctypes.WinDLL("ntdll").NtQuerySystemInformation(21, ctypes.byref(info), ctypes.sizeof(info),
                                                             ctypes.byref(got))  # SystemFileCacheInformation
    return {"active": info.CurrentSize, "peak": info.PeakSize} if status == 0 else None


def _role(command: str) -> str:
    """What an app process is, from its command line: gui, sync, live, ..."""
    rest = command.split(APP, 1)[1].split()
    words = [w for i, w in enumerate(rest) if not w.startswith("-") and (i == 0 or rest[i - 1] != "--source")]
    return " ".join(words[:2]) if words[:1] == ["admin"] else (words[0] if words else "?")


def app_processes() -> list[dict]:
    """The app's processes but this one: {pid, role, command}. A virtual environment starts each
    through a small launcher with the same command line, whose child does the work: the child is
    listed."""
    from . import proc

    rows = [(pid, ppid, cmd) for pid, ppid, cmd in proc.processes() if f"-m {APP}" in cmd or f"{APP}.exe" in cmd]
    launchers = {ppid for pid, ppid, cmd in rows if any(p == ppid and c == cmd for p, _, c in rows)}
    return [{"pid": pid, "role": _role(cmd), "command": cmd} for pid, ppid, cmd in rows
            if pid not in launchers and pid != os.getpid()]


def measure(seconds: float = 5.0) -> dict:
    """{processes: [{pid, role, private, working_set, read, read_per_s}], system: {...}, cache: {...}}:
    each process's own memory and disk reads (read_per_s over `seconds`; None for 0), and the
    machine's memory with the file cache apart (active: held; standby: available)."""
    procs = app_processes()
    first = {p["pid"]: _process_counters(p["pid"]) for p in procs}
    if seconds > 0:
        time.sleep(seconds)
    out = []
    for p in procs:
        now = _process_counters(p["pid"])
        if now is None:
            continue  # ended meanwhile, or not ours to look at
        was = first.get(p["pid"])
        rate = (now["read"] - was["read"]) / seconds if seconds > 0 and was and now["read"] is not None \
            and was["read"] is not None else None
        out.append({**p, **now, "read_per_s": rate})
    system, active = _system(), _active_cache()
    cache = None
    if system is not None and active is not None:
        cache = {"active": active["active"], "peak": active["peak"],
                 "standby": max(0, system["cache"] - active["active"])}
    return {"processes": out, "system": system, "cache": cache}


def _mb(n) -> str:
    return "?" if n is None else f"{n / MB:,.0f} MB"


def report(m: dict) -> str:
    """measure()'s result as lines of text."""
    lines = ["the app's processes (their own memory):"]
    for p in sorted(m["processes"], key=lambda p: -p["private"]):
        rate = "" if p["read_per_s"] is None else f", reading {p['read_per_s'] / MB:,.1f} MB/s now"
        lines.append(f"  {p['role']:<14} pid {p['pid']:<6} private {_mb(p['private']):>9}  working set "
                     f"{_mb(p['working_set']):>9}  read {_mb(p['read'])} since it started{rate}")
    if not m["processes"]:
        lines.append("  none running")
    s, c = m["system"], m["cache"]
    if s is None or c is None:
        lines.append("Windows' file cache: not measured here (Windows only)")
        return "\n".join(lines)
    lines += ["Windows' file cache (the system's, not the app's):",
              f"  active  {_mb(c['active']):>9}  held in RAM (peak since boot {_mb(c['peak'])})",
              f"  standby {_mb(c['standby']):>9}  handed back whenever anything needs it (counts as available)",
              f"machine: {_mb(s['available'])} available of {_mb(s['total'])}; committed "
              f"{_mb(s['commit'])} of {_mb(s['commit_limit'])}"]
    return "\n".join(lines)
