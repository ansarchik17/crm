"""Create a checked, private SQLite backup without modifying the source schema."""

import argparse
import os
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path
from time import monotonic


class CallbackBackupError(Exception):
    """Only a fixed code; filesystem and database details stay private."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_ERRORS = {
    "source_missing": "Журнал callback отсутствует; копия не создана.",
    "source_invalid": "Журнал callback недоступен или не прошёл проверку; копия не создана.",
    "destination_exists": "Файл назначения уже существует; перезапись запрещена.",
    "backup_failed": "Копия журнала callback не подтверждена.",
    "invalid_arguments": "Укажите --destination; --source можно задать отдельно.",
}


def _validate_store(connection: sqlite3.Connection, error_code: str) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise CallbackBackupError(error_code)
    table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
        ("callback_operations",),
    ).fetchone()
    if table is None:
        raise CallbackBackupError(error_code)


def _exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def backup_callback_store(source: str | Path, destination: str | Path) -> None:
    """Back up a read-only source into one new file, never reusing a destination.

    The destination parent must already exist. SQLite's backup API includes
    committed WAL data; the source is not opened through the migrating store.
    """
    descriptor = None
    created_identity = None
    completed = False
    destination_path = None
    try:
        source_path = Path(source)
        destination_path = Path(destination)
        try:
            source_info = source_path.lstat()
        except FileNotFoundError:
            raise CallbackBackupError("source_missing") from None
        if not stat.S_ISREG(source_info.st_mode):
            raise CallbackBackupError("source_invalid")

        try:
            source_connection = sqlite3.connect(
                source_path.absolute().as_uri() + "?mode=ro", uri=True,
                timeout=5, isolation_level=None,
            )
            with closing(source_connection):
                _validate_store(source_connection, "source_invalid")
                # Refuse existing links, files and SQLite sidecars before SQLite
                # can open any of them. O_EXCL makes the main-file check atomic.
                if any(_exists(Path(str(destination_path) + suffix))
                       for suffix in ("", "-journal", "-wal", "-shm")):
                    raise CallbackBackupError("destination_exists")
                try:
                    descriptor = os.open(destination_path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                                         0o600)
                except FileExistsError:
                    raise CallbackBackupError("destination_exists") from None
                created_info = os.fstat(descriptor)
                created_identity = (created_info.st_dev, created_info.st_ino)
                os.fchmod(descriptor, 0o600)
                with closing(sqlite3.connect(destination_path.absolute().as_uri() + "?mode=rw", uri=True,
                                             timeout=5, isolation_level=None)) as target:
                    target.execute("PRAGMA synchronous = FULL")
                    deadline = monotonic() + 30

                    def progress(_status: int, _remaining: int, _total: int) -> None:
                        if monotonic() >= deadline:
                            raise CallbackBackupError("backup_failed")

                    source_connection.backup(target, pages=128, progress=progress, sleep=0.05)
                    # Deliver a standalone database even when the source uses WAL.
                    if target.execute("PRAGMA journal_mode = DELETE").fetchone() != ("delete",):
                        raise CallbackBackupError("backup_failed")
                    _validate_store(target, "backup_failed")
                os.fsync(descriptor)
                completed = True
        except sqlite3.Error:
            code = "source_invalid" if created_identity is None else "backup_failed"
            raise CallbackBackupError(code) from None
    except CallbackBackupError:
        raise
    except (OSError, TypeError, ValueError):
        raise CallbackBackupError("backup_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not completed and created_identity is not None and destination_path is not None:
            try:
                info = destination_path.lstat()
                if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == created_identity:
                    destination_path.unlink()
            except OSError:
                pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise CallbackBackupError("invalid_arguments")


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _ArgumentParser(description="Проверенная копия журнала callback через SQLite backup API.")
        parser.add_argument("--source", type=Path, default=Path("runtime/callbacks.sqlite3"))
        parser.add_argument("--destination", type=Path, required=True)
        arguments = parser.parse_args(argv)
        backup_callback_store(arguments.source, arguments.destination)
    except CallbackBackupError as error:
        print(_ERRORS[error.code], file=sys.stderr)
        return 1
    except Exception:
        print(_ERRORS["backup_failed"], file=sys.stderr)
        return 1
    print("Копия журнала callback создана и проверена.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
