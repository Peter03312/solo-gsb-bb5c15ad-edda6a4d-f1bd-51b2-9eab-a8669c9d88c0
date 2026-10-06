"""FastAPI 入口：统一错误响应、启动初始化、健康检查。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import seed
from .config import settings
from .errors import ApiError
from .routers import admin, samples


@asynccontextmanager
async def lifespan(app: FastAPI):
    result = seed.initialize()
    if result is not None:
        print(f"[startup] seed completed: {result}")
    else:
        print("[startup] schema initialized (seeding disabled)")
    yield


app = FastAPI(
    title="自来水厂取样合规复核服务",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "维护分区必测指标与左闭右开生效限值；样本读数乐观锁录入/更正（请求号幂等）；"
        "必测齐全且每项恰有一条生效限值方可发布，任一超限即不合格；"
        "报告保存读数、限值依据与结论快照，按读数版本留痕。"
    ),
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


def _sanitize_errors(errors: list[dict]) -> list[dict]:
    """pydantic v2 的 errors() 在 ctx 中可能携带异常对象，递归转为字符串。"""
    safe: list[dict] = []
    for err in errors:
        item = {}
        for key, value in err.items():
            if isinstance(value, dict):
                item[key] = {k: (str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v)
                             for k, v in value.items()}
            else:
                item[key] = value
        safe.append(item)
    return safe


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "request payload validation failed",
                "details": {"errors": _sanitize_errors(exc.errors())},
            }
        },
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


app.include_router(admin.router)
app.include_router(samples.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
