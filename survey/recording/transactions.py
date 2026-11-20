from contextlib import contextmanager
import sqlite3


class CommitRejected(RuntimeError):
    """The workday fixture rejected the commit at the selected boundary."""


class StationDatabase:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys = ON')

    @contextmanager
    def atomic(self, *, reject_commit=False):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self.connection
            if reject_commit:
                raise CommitRejected('custody transaction rejected before commit')
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def initialize(self, installers):
        with self.atomic() as conn:
            for install in installers:
                install(conn)

    def disconnect(self):
        self.connection.close()
