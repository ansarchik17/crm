"""SQLite backup and synthetic restore; no real runtime files or CRM traffic."""

import asyncio
import importlib.util
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from voicecrm.crm.callback_store import CallbackOperation, CallbackStore
from voicecrm.crm.callbacks import CallbackService, callback_task_payload


_SPEC = importlib.util.spec_from_file_location(
    "backup_callback_store", Path(__file__).resolve().parents[1] / "scripts/backup_callback_store.py",
)
backup_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backup_module)


def populated_store(path):
    store = CallbackStore(path)
    operations = {}
    for status in ("draft", "canceled", "dispatching", "unconfirmed", "succeeded", "failed"):
        operation = CallbackOperation(
            id=str(uuid4()), status="draft", phone="+12025550199",
            summary=f"Synthetic request for {status}", contact_id="synthetic_contact",
            contact_name="Synthetic Contact", assigned_user_id="synthetic_employee",
        )
        store.create(operation)
        if status == "canceled":
            store.transition(operation.id, expected={"draft"}, status="canceled")
        elif status != "draft":
            store.transition(operation.id, expected={"draft"}, status="dispatching")
            if status != "dispatching":
                store.transition(operation.id, expected={"dispatching"}, status=status,
                                 task_id="synthetic_task" if status == "succeeded" else None,
                                 error="write_unconfirmed" if status == "unconfirmed" else
                                 "forbidden" if status == "failed" else None)
        operations[status] = store.get(operation.id)
    return store, operations


class ReadOnlyCRM:
    def __init__(self, records=()):
        self.records = list(records)
        self.finds = []
        self.posts = []

    async def find_callback_tasks(self, name):
        self.finds.append(name)
        return [record for record in self.records if record.name == name]

    async def create_task(self, task):
        self.posts.append(task)
        raise AssertionError("Restored claimed operations must not create a task")


def test_backup_preserves_all_statuses_receipts_and_read_only_source(tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _, operations = populated_store(source)
    source_before = source.read_bytes()
    source.chmod(0o400)

    backup_module.backup_callback_store(source, destination)

    assert source.read_bytes() == source_before
    assert stat.S_IMODE(source.stat().st_mode) == 0o400
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    restored = CallbackStore(destination)
    assert {status: restored.get(operation.id) for status, operation in operations.items()} == operations
    with closing(sqlite3.connect(destination)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert {path.name for path in tmp_path.iterdir()} == {"source.sqlite3", "backup.sqlite3"}


@pytest.mark.parametrize("remote_saved", [False, True])
def test_restored_claims_only_reconcile_and_never_repeat_a_post(tmp_path, remote_saved):
    source, destination = tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3"
    _, operations = populated_store(source)
    backup_module.backup_callback_store(source, destination)
    records = [SimpleNamespace(id=f"task_{status}", **callback_task_payload(operations[status]).model_dump())
               for status in ("dispatching", "unconfirmed")] if remote_saved else []
    crm = ReadOnlyCRM(records)
    callbacks = CallbackService(crm, CallbackStore(destination), "synthetic_employee")

    async def check():
        for status in ("dispatching", "unconfirmed", "succeeded", "canceled", "failed"):
            operation = operations[status]
            confirmed = await callbacks.confirm(operation.id)
            reconciled = await callbacks.reconcile(operation.id)
            assert confirmed == reconciled
            if status in ("dispatching", "unconfirmed"):
                assert confirmed.status == ("succeeded" if remote_saved else "unconfirmed")
            else:
                assert confirmed == operation
        assert crm.posts == []

    asyncio.run(check())
    assert len(crm.finds) == (2 if remote_saved else 4)


def test_backup_reads_committed_wal_data_and_produces_one_standalone_file(tmp_path):
    source, destination = tmp_path / "wal.sqlite3", tmp_path / "backup.sqlite3"
    _, operations = populated_store(source)
    draft = operations["draft"]
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE callback_operations SET summary = ? WHERE id = ?",
                       ("Synthetic committed WAL update", draft.id))
        writer.commit()
        assert Path(str(source) + "-wal").exists()
        backup_module.backup_callback_store(source, destination)
        assert CallbackStore(destination).get(draft.id).summary == "Synthetic committed WAL update"
    assert not Path(str(destination) + "-wal").exists()
    assert not Path(str(destination) + "-shm").exists()


def test_backup_does_not_migrate_the_source_or_backup_schema(tmp_path):
    source, destination = tmp_path / "legacy.sqlite3", tmp_path / "backup.sqlite3"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE callback_operations (id TEXT PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO callback_operations VALUES ('synthetic_operation', 'unconfirmed')")
        connection.commit()
    before = source.read_bytes()
    backup_module.backup_callback_store(source, destination)
    assert source.read_bytes() == before
    for path in (source, destination):
        with closing(sqlite3.connect(path)) as connection:
            assert [row[1] for row in connection.execute("PRAGMA table_info(callback_operations)")] == ["id", "status"]
            assert connection.execute("SELECT * FROM callback_operations").fetchall() == [
                ("synthetic_operation", "unconfirmed"),
            ]


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling_symlink", "sidecar"])
def test_existing_destination_or_links_are_never_overwritten(tmp_path, kind):
    source, destination = tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3"
    populated_store(source)
    protected = tmp_path / "protected"
    protected.write_bytes(b"synthetic existing content")
    protected.chmod(0o644)
    if kind == "file":
        destination.write_bytes(b"synthetic previous backup")
    elif kind == "symlink":
        destination.symlink_to(protected)
    elif kind == "dangling_symlink":
        destination.symlink_to(tmp_path / "absent_target")
    else:
        Path(str(destination) + "-wal").write_bytes(b"synthetic previous WAL")
    snapshot = {path.name: (path.lstat().st_mode, path.readlink() if path.is_symlink() else path.read_bytes())
                for path in tmp_path.iterdir()}
    with pytest.raises(backup_module.CallbackBackupError, match="^destination_exists$"):
        backup_module.backup_callback_store(source, destination)
    assert {path.name: (path.lstat().st_mode, path.readlink() if path.is_symlink() else path.read_bytes())
            for path in tmp_path.iterdir()} == snapshot


@pytest.mark.parametrize("kind,code", [
    ("missing", "source_missing"), ("corrupt", "source_invalid"),
    ("empty", "source_invalid"), ("other_schema", "source_invalid"),
    ("directory", "source_invalid"), ("symlink", "source_invalid"),
])
def test_bad_source_never_creates_a_destination_or_an_empty_source(tmp_path, kind, code):
    source, destination = tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3"
    if kind == "corrupt":
        source.write_bytes(b"synthetic corrupt database, not SQLite")
    elif kind == "empty":
        source.touch()
    elif kind == "other_schema":
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("CREATE TABLE unrelated (id TEXT)")
    elif kind == "directory":
        source.mkdir()
    elif kind == "symlink":
        source.symlink_to(tmp_path / "absent_database")
    before = {path.name for path in tmp_path.iterdir()}
    with pytest.raises(backup_module.CallbackBackupError, match=f"^{code}$") as error:
        backup_module.backup_callback_store(source, destination)
    assert str(error.value) == code and error.value.__cause__ is None
    assert not destination.exists()
    assert {path.name for path in tmp_path.iterdir()} == before
    if kind == "missing":
        assert not source.exists()


def test_connections_are_closed_and_source_connection_is_read_only(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3"
    populated_store(source)
    original_connect = sqlite3.connect
    opened = []

    def connect(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        opened.append(connection)
        if len(opened) == 1:
            assert database.endswith("?mode=ro")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("PRAGMA user_version = 123")
        return connection

    monkeypatch.setattr(backup_module.sqlite3, "connect", connect)
    backup_module.backup_callback_store(source, destination)
    assert len(opened) == 2
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_failed_target_validation_removes_only_the_new_incomplete_file(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3"
    populated_store(source)
    before = source.read_bytes()
    validate = backup_module._validate_store

    def fail_target(connection, code):
        if code == "backup_failed":
            raise backup_module.CallbackBackupError("backup_failed")
        validate(connection, code)

    monkeypatch.setattr(backup_module, "_validate_store", fail_target)
    with pytest.raises(backup_module.CallbackBackupError, match="^backup_failed$"):
        backup_module.backup_callback_store(source, destination)
    assert source.read_bytes() == before
    assert {path.name for path in tmp_path.iterdir()} == {"source.sqlite3"}


def test_cli_reports_fixed_messages_without_paths_or_operation_contents(tmp_path, capsys):
    source, destination = tmp_path / "private_source.sqlite3", tmp_path / "private_backup.sqlite3"
    populated_store(source)
    assert backup_module.main(["--source", str(source), "--destination", str(destination)]) == 0
    output = capsys.readouterr()
    assert output.out == "Копия журнала callback создана и проверена.\n" and output.err == ""
    assert backup_module.main(["--source", str(source), "--destination", str(destination)]) == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == "Файл назначения уже существует; перезапись запрещена.\n"
    assert backup_module.main(["--source", str(tmp_path / "absent"), "--destination", str(tmp_path / "new")]) == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == "Журнал callback отсутствует; копия не создана.\n"
    assert backup_module.main(["--unexpected", str(source)]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "private_source" not in output.err and "usage:" not in output.err


def test_cli_default_source_is_relative_to_working_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "runtime" / "callbacks.sqlite3"
    populated_store(source)
    destination = tmp_path / "backup.sqlite3"
    assert backup_module.main(["--destination", str(destination)]) == 0
    assert destination.exists()
    assert "runtime" not in capsys.readouterr().out
