import sqlite3

from .contracts import Scope


def authorize(connection: sqlite3.Connection, scope: Scope, *, write: bool = False):
    permission = connection.execute(
        "SELECT can_write FROM permissions WHERE user_id=? AND enterprise_id=?",
        (scope.user_id, scope.enterprise_id),
    ).fetchone()
    if permission is None or (write and not permission[0]):
        raise PermissionError("current user cannot perform this operation in this enterprise")
