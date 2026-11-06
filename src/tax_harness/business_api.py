import sqlite3
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .business import SqliteBusinessReader
from .business_writes import SqliteBusinessWriter, VerificationFailure
from .contracts import BankFilters, Error, Identifier, InvoiceFilters, InvoiceSaveRequest, TaskCloseRequest, ToolResult
from .database import Database
from .workbench import ScopedRequest, call


def run_business_action(action, *args, failure_message="业务数据查询失败，请检查数据库。"):
    try:
        return call(action, *args)
    except VerificationFailure as exc:
        error = Error(code="tool_failed", message=str(exc))
        return JSONResponse(status_code=500, content=error.model_dump())
    except sqlite3.Error:
        error = Error(code="tool_failed", message=failure_message)
        return JSONResponse(status_code=500, content=error.model_dump())


def mount_business_api(app, database: Database):
    reader = SqliteBusinessReader(database)
    writer = SqliteBusinessWriter(database)
    app.state.business_reader = reader
    app.state.business_writer = writer
    router = APIRouter(tags=["业务数据"], responses={403: {"model": Error}, 500: {"model": Error}})
    mutation_errors = {404: {"model": Error}, 409: {"model": Error}}

    @router.get("/api/invoices", response_model=ToolResult)
    def invoices(scope: ScopedRequest, invoice_id: Identifier | None = None,
                 seller: Identifier | None = None, item_name: Identifier | None = None):
        filters = InvoiceFilters(invoice_id=invoice_id, seller=seller, item_name=item_name)
        return run_business_action(reader.list_invoices, scope, filters)

    @router.get("/api/bank-transactions", response_model=ToolResult)
    def bank_transactions(scope: ScopedRequest, transaction_id: Identifier | None = None,
                          direction: Literal["income", "expense"] | None = None):
        filters = BankFilters(transaction_id=transaction_id, direction=direction)
        return run_business_action(reader.list_bank_transactions, scope, filters)

    @router.get("/api/invoices/{invoice_id}", response_model=ToolResult, responses={404: {"model": Error}})
    def invoice(scope: ScopedRequest, invoice_id: Identifier):
        return run_business_action(reader.get_record, scope, invoice_id)

    @router.patch("/api/invoices/{invoice_id}", response_model=ToolResult, responses=mutation_errors)
    def save_invoice(scope: ScopedRequest, invoice_id: Identifier, request: InvoiceSaveRequest):
        return run_business_action(writer.update_fields, scope, invoice_id, request,
                                   failure_message="保存未能确认，请核对原操作编号和发票状态。")

    @router.get("/api/operations/{operation_id}", response_model=ToolResult, responses={404: {"model": Error}})
    def operation(scope: ScopedRequest, operation_id: Identifier):
        return run_business_action(writer.get_operation, scope, operation_id)

    @router.post("/api/operations/{operation_id}/reconcile", response_model=ToolResult, responses={404: {"model": Error}})
    def reconcile(scope: ScopedRequest, operation_id: Identifier):
        return run_business_action(writer.reconcile_operation, scope, operation_id,
                                   failure_message="无法核验保存结果，请稍后查询原操作。")

    @router.patch("/api/tasks/{task_id}/status", responses=mutation_errors)
    def close_task(scope: ScopedRequest, task_id: Identifier, request: TaskCloseRequest):
        return run_business_action(writer.close_task, scope, task_id, request,
                                   failure_message="任务关闭未能确认，请重新读取任务状态。")

    app.include_router(router)
