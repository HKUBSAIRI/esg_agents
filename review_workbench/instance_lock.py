"""OS-released exclusive server lock, so a second server cannot interrupt active runs."""
import os


class InstanceLock:
    def __init__(self, directory):
        self.path = directory / "server.lock"
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0"); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close(); self.file = None
            raise ValueError("此数据目录已有工作台运行。请直接打开已有页面，或指定不同 --data-dir。") from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
