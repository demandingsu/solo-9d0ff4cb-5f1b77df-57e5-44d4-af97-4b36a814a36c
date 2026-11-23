"""FastAPI application: women's 1500 m record-progression archive."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import service
from app.database import init_db, migrate_db
from app.errors import AppError
from app.schemas import (
    AnnouncementIn,
    ErrorResponse,
    ProgressionEntryOut,
    RegistryResultOut,
    VersionDetail,
    VersionSummary,
)
from app.timeutil import utc_now


def _db_path() -> str:
    return os.environ.get("APP_DB_PATH", "/data/records.db")


@asynccontextmanager
async def lifespan(_: FastAPI):
    path = _db_path()
    init_db(path)
    # An existing v1 volume is upgraded here, in one transaction; a failed
    # upgrade leaves the volume usable by the previous service and is
    # retried on the next startup.
    migrate_db(path)
    service.ensure_baseline(path)
    yield


app = FastAPI(
    title="女子 1500 米纪录沿革档案",
    version="1.0.0",
    description=(
        "接收成绩公告并生成女子 1500 米纪录沿革。\n\n"
        "- 每版按当时有效成绩的完赛时刻、成绩编号排序；"
        "列出首个纪录（first）、刷新（refresh）与追平（tie）。\n"
        "- 补登需成绩编号、选手、完赛时刻与毫秒用时；撤销仅需成绩编号。\n"
        "- 提交必须指定当前版号；同一版最多发布一份公告。\n"
        "- 补登或撤销后从最早有改动的完赛时刻起重算，旧版始终可按版号回查。\n"
        "- 每版只新增本次补登/撤销涉及的登记记录，有效成绩不再整版重复存储；"
        "挂载旧版数据卷启动时自动合并历史快照。\n"
        "- 公告与沿革在同一事务中生效；过期提交或写入失败不会留下部分结果。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(AppError)
async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "validation_error",
                "message": "请求内容不合法",
                "errors": jsonable_encoder(exc.errors()),
            }
        },
    )


@app.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok", "time": utc_now()}


@app.post(
    "/announcements",
    response_model=VersionDetail,
    status_code=201,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "基于版号过期或编号冲突"},
        422: {"model": ErrorResponse},
    },
    summary="提交成绩公告并发布新版",
)
def publish_announcement(announcement: AnnouncementIn) -> dict:
    version = service.publish(_db_path(), announcement)
    return service.get_version(_db_path(), version)


@app.get(
    "/versions",
    response_model=list[VersionSummary],
    summary="按版号排序列出全部已发布版本",
)
def list_versions(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=service.MAX_PAGE_SIZE),
) -> list[dict]:
    return service.list_versions(_db_path(), offset=offset, limit=limit)


@app.get(
    "/versions/latest",
    response_model=VersionDetail,
    responses={404: {"model": ErrorResponse}},
    summary="查看当前最新版（含沿革与有效成绩）",
)
def get_latest_version() -> dict:
    latest = service.latest_version(_db_path())
    if latest == 0:
        raise AppError(404, "no_versions", "尚无已发布版本")
    return service.get_version(_db_path(), latest)


@app.get(
    "/versions/{version}",
    response_model=VersionDetail,
    responses={404: {"model": ErrorResponse}},
    summary="回查指定版的沿革快照",
)
def get_version(version: int) -> dict:
    return service.get_version(_db_path(), version)


@app.get(
    "/versions/{version}/progression",
    response_model=list[ProgressionEntryOut],
    responses={404: {"model": ErrorResponse}},
    summary="回查指定版的纪录沿革",
)
def get_version_progression(version: int) -> list[dict]:
    entries = service.get_progression(_db_path(), version)
    return [
        {
            "seq": entry.seq,
            "kind": entry.kind,
            "result_id": entry.result_id,
            "athlete": entry.athlete,
            "finish_time": entry.finish_time,
            "duration_ms": entry.duration_ms,
        }
        for entry in entries
    ]


@app.get(
    "/results",
    response_model=list[RegistryResultOut],
    summary="查看成绩登记簿（当前有效或已撤销）",
)
def list_results(
    status: Literal["active", "withdrawn", "all"] = Query(
        "active", description="active=当前有效；withdrawn=已撤销；all=全部"
    ),
) -> list[dict]:
    return service.list_results(_db_path(), status=status)
