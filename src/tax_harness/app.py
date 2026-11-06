import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

from . import contracts
from .access import authorize
from .agent_api import mount_agent
from .business_api import mount_business_api
from .contracts import Error, Month, Scope
from .database import Database
from .model_adapter import ModelAdapter
from .workbench import mount_workbench


def create_app(db_path: str | Path | None = None, *, model: ModelAdapter | None = None) -> FastAPI:
    database = Database(db_path or os.getenv("HARNESS_DB_PATH", "data/harness.sqlite3"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialize()
        yield

    app = FastAPI(title="Tax Harness A1", version="0.1.0", lifespan=lifespan)
    app.state.database = database

    @app.get("/health")
    def health():
        with database.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        return {"status": "ok", "stage": "A1", "schema_version": version}

    @app.get("/api/scope", response_model=Scope, responses={403: {"model": Error}})
    def resolve_scope(enterprise_id: str, month: Month,
                      x_demo_user: Annotated[str, Header(min_length=1, max_length=128)]):
        scope = Scope(user_id=x_demo_user, enterprise_id=enterprise_id, month=month)
        with database.connect() as connection:
            try:
                authorize(connection, scope)
            except PermissionError as exc:
                return JSONResponse(status_code=403, content=Error(code="forbidden", message=str(exc)).model_dump())
        return scope

    @app.get("/api/contracts")
    def contract_schemas():
        names = ("Scope", "MessageRequest", "TurnResponse", "ToolResult", "Error", "Citation",
                 "InvoicePatch", "InvoiceSaveRequest", "TaskCloseRequest", "TaskCorrectionRequest", "InvoiceFilters", "BankFilters", "Memory", "RuleHit", "MutationReceipt",
                 "ContextRequest", "ContextBundle")
        return {"version": "1", "schemas": {name: getattr(contracts, name).model_json_schema() for name in names}}

    mount_business_api(app, database)
    mount_workbench(app, database)
    mount_agent(app, model)
    return app


app = create_app()
