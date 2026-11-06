import os
from typing import Annotated

from fastapi import Header
from fastapi.responses import JSONResponse

from .business_api import run_business_action
from .contracts import Error, Identifier, MessageRequest, TaskCorrectionRequest, TurnResponse
from .executor import TurnExecutor
from .model_adapter import ModelAdapter, ModelUnavailable, configured_model
from .tasks import active_task_status, correct_task, read_task
from .workbench import ScopedRequest


def mount_agent(app, model: ModelAdapter | None):
    model = model if model is not None else configured_model()
    budget = int(os.getenv("HARNESS_INPUT_BUDGET", "12000"))
    if budget <= 0:
        raise ValueError("HARNESS_INPUT_BUDGET must be positive")
    executor = TurnExecutor(app.state.database, model, app.state.business_reader, app.state.business_writer,
                            app.state.memory_store, app.state.rule_store, budget=budget)
    app.state.turn_executor = executor

    @app.get("/api/tasks/active", tags=["对话"])
    def active_task(scope: ScopedRequest):
        return run_business_action(active_task_status, app.state.database, scope)

    @app.get("/api/tasks/{task_id}", tags=["对话"])
    def task(scope: ScopedRequest, task_id: Identifier):
        return run_business_action(read_task, app.state.database, scope, task_id)

    @app.patch("/api/tasks/{task_id}", tags=["对话"])
    def update_task(scope: ScopedRequest, task_id: Identifier, request: TaskCorrectionRequest):
        return run_business_action(correct_task, app.state.database, scope, task_id, request)

    @app.post("/api/messages", response_model=TurnResponse, tags=["对话"], responses={503: {"model": Error}})
    def messages(request: MessageRequest, x_demo_user: Annotated[Identifier, Header()]):
        try:
            return run_business_action(executor.execute, x_demo_user, request,
                                       failure_message="本轮处理状态未能确认，请按原请求编号核对。")
        except ModelUnavailable as exc:
            error = Error(code="tool_failed", message=str(exc))
            return JSONResponse(status_code=503, content=error.model_dump())
