"""Cross-platform, OS-managed process lock (automatically released on crash)."""
from pathlib import Path
import os
from contextlib import contextmanager


@contextmanager
def process_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("Another monitor owns this state file") from None
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("Another monitor owns this state file") from None
        yield
    finally:
        # Closing releases OS lock, including on exception / interrupted process.
        f.close()
