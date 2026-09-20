"""Test-wide safety net. Every test module imports this first (via `support` or directly).

1. Forces every data path used by the app modules into a throwaway temp dir.
2. Fails loudly if anything still touches the REAL data/, boards/ or the pause
   flag: file open/stat/mkdir/rename/unlink all raise RealDataAccessError.
Run the suite with: python -m unittest discover -s tests
"""

import atexit
import builtins
import io
import os
import pathlib
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TMP = Path(tempfile.mkdtemp(prefix="hiit-test-"))
TMP_DATA = TMP / "data"
TMP_DATA.mkdir()
(TMP / "boards").mkdir()
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

ORIG = {"unlink": os.unlink, "rmdir": os.rmdir}  # unguarded, only for cleaning up after a guard failure

_REAL = [os.path.normcase(os.path.abspath(REPO / n)) for n in ("data", "boards", "automation_paused.flag")]


class RealDataAccessError(BaseException):
    """BaseException on purpose: a production `except Exception` must not swallow it."""


def check(path):
    """Raise if `path` is inside the real data/ or boards/ (or is the real pause flag)."""
    try:
        p = os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(path))))
    except TypeError:
        return  # a file descriptor, not a path
    for root in _REAL:
        if p == root or p.startswith(root + os.sep):
            raise RealDataAccessError(f"test touched the REAL data path: {path}")


def _guarded(func, *path_args):
    def wrapper(*args, **kwargs):
        for i in path_args:
            if i < len(args):
                check(args[i])
        for key in ("file", "path", "src", "dst", "target"):
            if key in kwargs:
                check(kwargs[key])
        return func(*args, **kwargs)

    return wrapper


def _install():
    builtins.open = io.open = _guarded(io.open, 0)
    for name, idx in (("replace", (0, 1)), ("rename", (0, 1)), ("remove", (0,)), ("unlink", (0,)),
                      ("makedirs", (0,)), ("listdir", (0,))):
        setattr(os, name, _guarded(getattr(os, name), *idx))
    # Python 3.10's Path.open bypasses io.open (pre-bound accessor), so every Path I/O method is wrapped.
    for name in ("stat", "mkdir", "unlink", "rmdir", "touch", "iterdir", "glob", "rglob",
                 "open", "read_text", "write_text", "read_bytes", "write_bytes"):
        setattr(pathlib.Path, name, _guarded(getattr(pathlib.Path, name), 0))
    for name in ("replace", "rename"):
        setattr(pathlib.Path, name, _guarded(getattr(pathlib.Path, name), 0, 1))


def _repoint():
    import analyze_workout, garmin_health, garmin_sync, pending_store, telegram_bot

    pending_store.ROOT = analyze_workout.ROOT = garmin_health.ROOT = telegram_bot.ROOT = TMP
    pending_store.PENDING_PATH = TMP_DATA / "pending.json"
    pending_store.ARCHIVE_PATH = TMP_DATA / "pending_archive.json"
    pending_store.BOARDS_DIR = telegram_bot.BOARDS_DIR = TMP / "boards"
    analyze_workout.WORKOUTS_PATH = TMP_DATA / "workouts.json"
    analyze_workout.LAST_RUN_PATH = telegram_bot.LAST_RUN_PATH = TMP_DATA / "last_run.json"
    analyze_workout.PAUSE_FLAG = telegram_bot.PAUSE_FLAG = TMP / "automation_paused.flag"
    telegram_bot.STATE_PATH = TMP_DATA / "telegram_state.json"
    garmin_health.STATE_PATH = TMP_DATA / "garmin_state.json"
    garmin_sync.DATA_DIR = TMP_DATA


_repoint()  # import the app first, under the normal open(), then lock the doors
_install()
