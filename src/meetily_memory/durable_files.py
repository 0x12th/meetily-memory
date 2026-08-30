import errno
import os
from contextlib import suppress
from pathlib import Path

UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    {
        errno.EINVAL,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
)


def fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise
    finally:
        with suppress(OSError):
            os.close(directory_fd)


def durable_replace(temporary_path: Path, destination_path: Path) -> None:
    with temporary_path.open("rb") as temporary_file:
        os.fsync(temporary_file.fileno())
    os.replace(temporary_path, destination_path)  # noqa: PTH105
    fsync_directory(destination_path.parent)
