from .access import authorize
from .contracts import BankFilters, InvoiceFilters, Scope, ToolResult
from .database import Database


INVOICE_COLUMNS = "enterprise_id,invoice_id,invoice_date,seller,item_name,amount_cents,purpose,note,revision"


def read_invoice(connection, scope: Scope, object_id: str) -> dict:
    row = connection.execute(
        f"SELECT {INVOICE_COLUMNS} FROM invoices "
        "WHERE enterprise_id=? AND invoice_id=? AND substr(invoice_date,1,7)=?",
        (scope.enterprise_id, object_id, scope.month),
    ).fetchone()
    if row is None:
        raise KeyError("当前企业和月份下没有这张发票")
    return dict(row)


class SqliteBusinessReader:
    def __init__(self, database: Database):
        self.database = database

    def list_invoices(self, scope: Scope, filters: InvoiceFilters | None = None) -> ToolResult:
        filters = filters or InvoiceFilters()
        with self.database.connect() as connection:
            authorize(connection, scope)
            rows = connection.execute(
                f"""
                SELECT {INVOICE_COLUMNS}
                FROM invoices
                WHERE enterprise_id = :enterprise AND substr(invoice_date, 1, 7) = :month
                  AND (:invoice_id IS NULL OR invoice_id = :invoice_id)
                  AND (:seller IS NULL OR instr(seller, :seller) > 0)
                  AND (:item_name IS NULL OR instr(item_name, :item_name) > 0)
                ORDER BY invoice_date, invoice_id
                """,
                {"enterprise": scope.enterprise_id, "month": scope.month, **filters.model_dump()},
            ).fetchall()
        return ToolResult(status="succeeded", data={
            "records": [dict(row) for row in rows],
            "invoice_count": len(rows),
            "invoice_cents": sum(row["amount_cents"] for row in rows),
        })

    def list_bank_transactions(self, scope: Scope, filters: BankFilters | None = None) -> ToolResult:
        filters = filters or BankFilters()
        with self.database.connect() as connection:
            authorize(connection, scope)
            rows = connection.execute(
                """
                SELECT enterprise_id,transaction_id,transaction_date,direction,amount_cents,counterparty,note
                FROM bank_transactions
                WHERE enterprise_id = :enterprise AND substr(transaction_date, 1, 7) = :month
                  AND (:transaction_id IS NULL OR transaction_id = :transaction_id)
                  AND (:direction IS NULL OR direction = :direction)
                ORDER BY transaction_date, transaction_id
                """,
                {"enterprise": scope.enterprise_id, "month": scope.month, **filters.model_dump()},
            ).fetchall()
        totals = {"income": 0, "expense": 0}
        for row in rows:
            totals[row["direction"]] += row["amount_cents"]
        return ToolResult(status="succeeded", data={
            "records": [dict(row) for row in rows],
            "bank_count": len(rows),
            "income_cents": totals["income"],
            "expense_cents": totals["expense"],
        })

    def get_record(self, scope: Scope, object_id: str) -> ToolResult:
        with self.database.connect() as connection:
            authorize(connection, scope)
            record = read_invoice(connection, scope, object_id)
        return ToolResult(status="succeeded", object_id=object_id, data=record)
