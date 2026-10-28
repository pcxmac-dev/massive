"""Processes, and the lock files that keep one of a kind running (one fetching run per feed, one
live recorder per database and per machine).

A lock file names its owner by process id and the time that process started: the system reuses
process ids quickly (on Windows especially), so an id alone would make a lock left by a crashed
or rebooted process look held by whatever process has that id now. A lock is taken atomically,
with its owner written in it (a file written whole, then linked in place: never seen empty),
taken over only when its owner is gone (dead, or its id now belongs to another process), never
on age alone (a process that slept through the night still holds it), and removed only by its
owner. A lock that is empty or unreadable but only seconds old is held (one being written where
links are not available). Two processes taking over the same dead lock at the same instant can
still both win; that needs two crashes' worth of coincidence and a lock left behind, and is
accepted (the live recorder checks its lock before it ends other recorders).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

FRESH_SECONDS = 10.0  # an empty or unreadable lock younger than this is being written: held


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


def run_lock_path(db_path: Path, feed: str | None = None) -> Path:
    """A fetching run's lock beside the database: one feed's (`massive-fetch-alpaca.lock`: feeds
    fetch side by side, one run each), or the whole database's (`massive.lock`: a move of the data,
    which no run may write through, or a run from before the feeds ran apart)."""
    return db_path.with_suffix(".lock") if feed is None else db_path.with_name(f"{db_path.stem}-fetch-{feed}.lock")


def run_holders(db_path: Path) -> dict[str | None, int]:
    """The runs going now: {feed: the id of the process fetching it}, None for the whole database's
    lock (every feed is busy while it is held)."""
    out: dict[str | None, int] = {}
    whole = holder(run_lock_path(db_path))
    if whole is not None:
        out[None] = whole
    prefix = f"{db_path.stem}-fetch-"
    for path in db_path.parent.glob(f"{prefix}*.lock"):
        pid = holder(path)
        if pid is not None:
            out[path.name[len(prefix):-len(".lock")]] = pid
    return out


def acquire(path: Path) -> bool:
    """Take a lock file, unless a live owner holds it (a gone owner's lock is taken over)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        if _create(path, stamp()):
            return True
        if holder(path) is not None or _being_written(path):
            return False
        try:
            path.unlink()  # its owner is gone
        except FileNotFoundError:
            pass
    return False


def _create(path: Path, text: str) -> bool:
    """Create `path` holding `text`, atomically; False when it exists. The text is written to a
    file of this process's own, then linked in place (the link fails if the lock exists), so no
    other process can read the lock before its owner is in it. Where hard links are not available
    (a FAT drive): created exclusively, then written (_being_written covers the moment between)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.new")
    try:
        tmp.write_text(text)
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass  # no links here
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return True


def _being_written(path: Path) -> bool:
    """Whether a lock that names no owner (empty, unreadable) is only seconds old: its owner is
    writing it, so it is held, not left behind."""
    try:
        if path.read_text().split():
            return False  # it names its owner: holder() judged that
        return time.time() - path.stat().st_mtime < FRESH_SECONDS
    except OSError:
        return False  # gone meanwhile


def owns(path: Path) -> bool:
    """Whether this process holds a lock file."""
    try:
        return path.read_text().split()[:1] == [str(os.getpid())]
    except (OSError, IndexError):
        return False


def processes() -> list[tuple[int, int, str]]:
    """(pid, parent pid, command line) of the Python processes this user can see; [] when they
    cannot be listed. Windows: the system's own process table (CIM); elsewhere /proc."""
    import subprocess

    if os.name == "nt":
        script = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                  "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)\" }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=60,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        rows = []
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
        return rows
    rows = []
    for d in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if "python" in cmd:
            rows.append((int(d.name), ppid, cmd))
    return rows


def terminate(pid: int) -> bool:
    """End a process (TerminateProcess on Windows, SIGTERM elsewhere). Whether it was told to."""
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def ancestors(pid: int, rows: list[tuple[int, int, str]]) -> set[int]:
    """`pid` and its parents among `rows` (a venv's launcher runs the interpreter as its child)."""
    parent = {p: pp for p, pp, _ in rows}
    out, p = set(), pid
    while p and p not in out:
        out.add(p)
        p = parent.get(p)
    return out


def release(path: Path) -> None:
    """Remove a lock file this process holds (never one another process took since)."""
    try:
        if owns(path):
            path.unlink()
    except OSError:
        pass
