"""Processes, and the lock files that keep one of a kind running (one fetching run, one live
recorder per database and per machine).

A lock file names its owner by process id and the time that process started: the system reuses
process ids quickly (on Windows especially), so an id alone would make a lock left by a crashed
or rebooted process look held by whatever process has that id now. A lock is taken atomically,
taken over only when its owner is gone (dead, or its id now belongs to another process), never
on age alone (a process that slept through the night still holds it), and removed only by its
owner. Two processes taking over the same dead lock at the same instant can still both win; that
needs two crashes' worth of coincidence and a lock left behind, and is accepted.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def pid_alive(pid: int) -> bool:
    """Whether a process with this id is running (Windows and POSIX)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def started(pid: int) -> int | None:
    """When a process started, as a number that stays the same for its life (None when unknown)."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]  # creation, exit, kernel, user
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])  # starttime, after the command name
    except (OSError, ValueError, IndexError):
        return None  # no /proc (macOS): the id alone


def stamp() -> str:
    """What this process writes into a lock it holds: its id and its start."""
    return f"{os.getpid()} {started(os.getpid()) or 0}"


def holder(path: Path) -> int | None:
    """The process holding a lock file: its id, if it is alive and is the process that wrote it."""
    try:
        parts = path.read_text().split()
        pid, start = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (OSError, ValueError, IndexError):
        return None
    if not pid_alive(pid):
        return None
    if start:  # older locks name the id only
        now = started(pid)
        if now is not None and now != start:
            return None  # the id belongs to another process now
    return pid


def acquire(path: Path) -> bool:
    """Take a lock file, unless a live owner holds it (a gone owner's lock is taken over)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if holder(path) is not None:
                return False
            try:
                path.unlink()  # its owner is gone
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as f:
            f.write(stamp())
        return True
    return False


def owns(path: Path) -> bool:
    """Whether this process holds a lock file."""
    try:
        return path.read_text().split()[:1] == [str(os.getpid())]
    except (OSError, IndexError):
        return False


def release(path: Path) -> None:
    """Remove a lock file this process holds (never one another process took since)."""
    try:
        if owns(path):
            path.unlink()
    except OSError:
        pass
