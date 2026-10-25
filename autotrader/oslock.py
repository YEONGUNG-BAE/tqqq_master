"""OS-specific pieces behind one small interface (macOS/Linux and Windows).

Process locks: an exclusive lock on a lock file, held by the open file and
released by the OS when the process ends (crash included). POSIX uses
fcntl.flock; Windows uses msvcrt.locking on the file's first byte. There is
no fallback that skips locking: an OS without either refuses to run.

The live-check ownership marker is NOT a lock: it is a file created with
O_EXCL (both OSes) that stays until the check is finished or stopped, so a
dead process never silently releases ownership.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl


class Busy(BlockingIOError):
    """Another process holds the lock."""


def acquire(handle, blocking=False, poll=0.1):
    """Exclusive lock on an open file (mode "a" or "a+"). Raises Busy when
    non-blocking and held elsewhere."""
    if WINDOWS:
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if not blocking:
                    raise Busy(str(exc)) from None
                time.sleep(poll)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError as exc:
        raise Busy(str(exc)) from None


def release(handle):
    if WINDOWS:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def held(path, blocking=False):
    """Open `path` and hold its exclusive lock for the block."""
    with open(path, "a+") as handle:
        acquire(handle, blocking)
        try:
            yield handle
        finally:
            release(handle)


def replace(tmp, path, attempts=20, pause=0.05):
    """Atomic replace. Windows refuses while another process has the target
    open, so retry briefly; a final failure raises and the caller's state is
    unchanged (the temporary file is left, the old file still valid)."""
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(pause)


def private(path, directory=False):
    """Owner-only permissions. POSIX: chmod 700/600. Windows: chmod cannot
    express this; ACLs are set by the user (README) and checked with
    acl_report, so this returns False there (not verified here)."""
    if WINDOWS:
        return False
    os.chmod(path, 0o700 if directory else 0o600)
    return True


def user_name():
    if WINDOWS:
        return os.environ.get("USERNAME", "")
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


def state_base():
    """Per-user local folder for account locks (never a synced/shared folder)."""
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "tqqq_auto"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/tqqq_auto"
    return Path.home() / ".local/state/tqqq_auto"


BROAD_PRINCIPALS = ("Everyone", "BUILTIN\\Users", "Authenticated Users", "NT AUTHORITY\\Authenticated Users")


def acl_report(path, runner=None):
    """Windows only, read-only: icacls output and whether a broad group has
    access. On POSIX the mode bits are reported instead."""
    path = Path(path)
    if not path.exists():
        return {"path_exists": False}
    if not WINDOWS:
        mode = path.stat().st_mode & 0o777
        return {"path_exists": True, "mode": oct(mode), "private": not mode & 0o077}
    import subprocess
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    try:
        done = run(["icacls", str(path)])
        text = done.stdout or ""
    except Exception as exc:
        return {"path_exists": True, "checked": False, "error": type(exc).__name__}
    broad = [p for p in BROAD_PRINCIPALS if p.lower() in text.lower()]
    return {"path_exists": True, "checked": True, "broad_access": broad, "private": not broad}
