import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock


class Database:
    """One shared instance per application. One worker is required for coordination."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.mutation_lock = RLock()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.mutation_lock, self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                return
            if version != 0:
                raise RuntimeError(f"unsupported database schema version {version}")
            connection.executescript(Path(__file__).with_name("schema.sql").read_text("utf-8"))

    @contextmanager
    def write(self):
        with self.mutation_lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
