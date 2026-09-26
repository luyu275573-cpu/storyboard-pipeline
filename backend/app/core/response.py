"""统一响应体与 FastAPI 异常处理器。

响应格式对齐农牧项目：{code, message, data}，便于前端拦截器统一处理。
"""

from __future__ import annotations

import logging
from typing import Any, Generic, TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: str = ErrorCode.OK
    message: str = "success"
    data: T | None = None


def ok(data: Any = None, message: str = "success") -> dict[str, Any]:
    """成功响应。"""
    return {"code": ErrorCode.OK.value, "message": message, "data": data}


def fail(code: ErrorCode | str, message: str, data: Any = None, http_status: int = 400) -> JSONResponse:
    """失败响应。"""
    code_value = code.value if isinstance(code, ErrorCode) else code
    return JSONResponse(
        status_code=http_status,
        content={"code": code_value, "message": message, "data": data},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，把业务异常统一转为 {code,message,data}。"""

    @app.exception_handler(AppError)
    async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        # 预算/合规/挂起类是预期内的业务状态，用 warning；其余用 error
        expected = {
            ErrorCode.BUDGET_EXCEEDED,
            ErrorCode.QC_BLOCKED_COMPLIANCE,
            ErrorCode.PIPELINE_MAX_RETRY,
            ErrorCode.PIPELINE_SUSPENDED,
            ErrorCode.GATE_PENDING,
            ErrorCode.IDEMPOTENT_REPLAY,
        }
        log = logger.warning if exc.code in expected else logger.error
        log("业务异常 code=%s msg=%s detail=%s", exc.code, exc.message, exc.detail)
        return fail(exc.code, exc.message, exc.detail or None, exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic 校验失败：返回字段级错误，前端可直接定位
        return fail(ErrorCode.BAD_REQUEST, "请求参数校验失败", {"errors": exc.errors()}, 422)

    @app.exception_handler(Exception)
    async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常: %s", exc)
        # 不向前端泄漏内部堆栈，只给通用码；细节进日志
        return fail(ErrorCode.INTERNAL, "服务器内部错误", None, 500)
