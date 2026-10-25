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
import re
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
    with open(path, "a+", encoding="utf-8") as handle:
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


# Broad groups by SID (SDDL short form and full SID), so the check does not
# depend on the display language: Everyone, Authenticated Users, Users,
# Guests, Anonymous, Interactive, and a domain's Domain Users (RID 513).
BROAD_SIDS = {"WD": "Everyone", "S-1-1-0": "Everyone", "AU": "Authenticated Users",
              "S-1-5-11": "Authenticated Users", "BU": "Users", "S-1-5-32-545": "Users",
              "BG": "Guests", "S-1-5-32-546": "Guests", "AN": "Anonymous", "S-1-5-7": "Anonymous",
              "IU": "Interactive", "S-1-5-4": "Interactive", "DU": "Domain Users"}
_ACE = re.compile(r"\(([^()]*)\)")


def broad_access(sddl):
    """Broad groups granted access by the DACL of an SDDL string; None when
    there is no DACL to read. Deny entries are ignored (conservative); a null
    DACL (NO_ACCESS_CONTROL) grants everyone access."""
    for line in (sddl or "").splitlines():
        if "D:NO_ACCESS_CONTROL" in line:
            return ["Everyone"]
        dacl = re.search(r"D:[A-Z_]*(\(.*?)(?:S:[A-Z_]*\(|$)", line.strip())
        if not dacl:
            continue
        found = []
        for ace in _ACE.findall(dacl.group(1)):
            parts = ace.split(";")
            if len(parts) < 6 or parts[0] not in ("A", "OA", "XA", "ZA"):
                continue
            sid = parts[5].strip().upper()
            name = BROAD_SIDS.get(sid) or ("Domain Users" if re.fullmatch(r"S-1-5-21(-\d+){3}-513", sid) else None)
            if name and name not in found:
                found.append(name)
        return found
    return None


def _icacls_sddl(path, runner):
    """`icacls <path> /save <file>` writes the ACL as SDDL (UTF-16) with
    SIDs, independent of the display language. Returns (sddl, error)."""
    import subprocess
    import tempfile
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, timeout=10))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "acl.txt"
        try:
            done = run(["icacls", str(path), "/save", str(out)])
        except Exception as exc:
            return None, type(exc).__name__
        if done.returncode != 0:
            return None, f"icacls exit {done.returncode}"
        if not out.exists():
            return None, "icacls wrote nothing"
        raw = out.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    elif b"\x00" in raw:
        text = raw.decode("utf-16-le", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    return text, None


def acl_report(path, runner=None):
    """Read-only. POSIX: mode bits. Windows: the DACL as SDDL from icacls,
    judged by SID. A failed or unreadable query is reported as unchecked
    (private: None), never as private."""
    path = Path(path)
    if not path.exists():
        return {"path_exists": False}
    if not WINDOWS:
        mode = path.stat().st_mode & 0o777
        return {"path_exists": True, "mode": oct(mode), "private": not mode & 0o077}
    sddl, error = _icacls_sddl(path, runner)
    broad = broad_access(sddl) if sddl else None
    if broad is None:
        return {"path_exists": True, "checked": False, "private": None,
                "error": error or "no DACL in the icacls output"}
    return {"path_exists": True, "checked": True, "broad_access": broad, "private": not broad}
