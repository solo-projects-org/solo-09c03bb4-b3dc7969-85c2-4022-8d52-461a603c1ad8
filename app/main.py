"""FastAPI 应用：病例月报封版服务。

总院数仓汇编病例月报：各院区随时报送当月全量计数文件（JSON），工程师
在收齐后封版；封版是原子的整表替换，版号只能上升。新版未收齐前，下游
查询始终看到上一版整表。
"""

from __future__ import annotations

import os
from typing import Annotated, Union

from fastapi import Body, FastAPI, Path
from fastapi.responses import JSONResponse

from . import store as store_mod
from .schemas import (
    EX_FINALIZE,
    EX_UPLOAD_EMPTY,
    EX_UPLOAD_ROWS,
    MONTH_PATTERN,
    FileConflictResponse,
    FileListResponse,
    FileReceivedResponse,
    FinalizeRequest,
    FinalizedResponse,
    HealthResponse,
    MissingFilesResponse,
    MonthlyReportResponse,
    NotFoundResponse,
    ReportFilePayload,
    StoredFileResponse,
    VersionConflictResponse,
)

MonthPath = Annotated[
    str,
    Path(pattern=MONTH_PATTERN, description="统计月，格式 YYYY-MM", examples=["2026-10"]),
]
CampusPath = Annotated[
    str,
    Path(min_length=1, max_length=50, description="院区名称", examples=["东城院区"]),
]
FileVersionPath = Annotated[
    int,
    Path(ge=1, le=1_000_000, description="院区文件版本号（从 1 起）", examples=[1]),
]

DEFAULT_DB_PATH = "reports.db"


def create_app(db_path: str | None = None) -> FastAPI:
    """应用工厂：便于测试注入独立 SQLite 文件。"""
    db_path = db_path or os.environ.get("CASE_REPORT_DB", DEFAULT_DB_PATH)
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    store = store_mod.ReportStore(db_path)

    app = FastAPI(
        title="病例月报封版 API",
        description=(
            "总院数仓汇编病例月报：各院区按病例归属（乡镇 × 年龄组）以 JSON "
            "报送当月全量计数（零病例报空表），按统计月、院区、文件版本存入 "
            "SQLite；同版本重传计数相同则沿用、不同则报错。工程师提交月份、"
            "月报版号和每个应报院区的指定文件版本进行封版：缺件时报出缺失清单"
            "并保留上次封版结果；收齐后按乡镇和年龄组加总，一次替换整张月报。"
            "月报版号只能上升，并发封版不会回退；下游按月查询当前版号与整表。"
        ),
        version="1.0.0",
    )
    app.state.store = store

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["运维"],
        summary="健康检查",
    )
    def health() -> dict:
        return {"status": "ok", "service": "case-report-seal"}

    # -- 院区报送 -----------------------------------------------------------

    @app.put(
        "/api/v1/report-files/{month}/{campus}/{file_version}",
        response_model=FileReceivedResponse,
        responses={409: {"model": FileConflictResponse, "description": "同版本重传计数不同"}},
        tags=["报送"],
        summary="院区报送当月全量计数文件（幂等）",
    )
    def put_report_file(
        month: MonthPath,
        campus: CampusPath,
        file_version: FileVersionPath,
        payload: Annotated[
            ReportFilePayload,
            Body(
                openapi_examples={
                    "normal": {
                        "summary": "东城院区 2026-10 第 1 版（5 行计数）",
                        "value": EX_UPLOAD_ROWS,
                    },
                    "empty": {
                        "summary": "零病例院区报送空表",
                        "value": EX_UPLOAD_EMPTY,
                    },
                }
            ),
        ],
    ):
        try:
            return store.save_file(
                month, campus, file_version, [r.model_dump() for r in payload.rows]
            )
        except store_mod.FileVersionConflict as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "file_conflict",
                    "message": str(exc),
                    "month": exc.month,
                    "campus": exc.campus,
                    "file_version": exc.file_version,
                    "existing": exc.existing,
                    "rejected": exc.rejected,
                },
            )

    @app.get(
        "/api/v1/report-files/{month}",
        response_model=FileListResponse,
        tags=["报送"],
        summary="查看某统计月已收到的全部院区文件",
    )
    def list_report_files(month: MonthPath) -> dict:
        return {"month": month, "files": store.list_files(month)}

    @app.get(
        "/api/v1/report-files/{month}/{campus}/{file_version}",
        response_model=StoredFileResponse,
        responses={404: {"model": NotFoundResponse, "description": "文件未收到"}},
        tags=["报送"],
        summary="查看单个已收院区文件的内容",
    )
    def get_report_file(
        month: MonthPath, campus: CampusPath, file_version: FileVersionPath
    ):
        found = store.get_file(month, campus, file_version)
        if found is None:
            return JSONResponse(
                status_code=404,
                content={
                    "status": "not_found",
                    "message": (
                        f"{month} {campus} 文件版本 {file_version} 尚未收到"
                    ),
                },
            )
        return found

    # -- 封版 ---------------------------------------------------------------

    @app.post(
        "/api/v1/monthly-reports/finalize",
        response_model=FinalizedResponse,
        responses={
            409: {
                "model": Union[MissingFilesResponse, VersionConflictResponse],
                "description": "缺件（保留上次封版结果）或版号未高于当前版号",
            }
        },
        tags=["封版"],
        summary="封版：收齐指定文件版本后整表替换月报",
    )
    def finalize_report(
        payload: Annotated[
            FinalizeRequest,
            Body(
                openapi_examples={
                    "finalize_v1": {
                        "summary": "封版 2026-10 第 1 版月报（三个应报院区）",
                        "value": EX_FINALIZE,
                    }
                }
            ),
        ],
    ):
        try:
            return store.finalize(
                payload.month,
                payload.report_version,
                [f.model_dump() for f in payload.files],
            )
        except store_mod.MissingFiles as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "missing_files",
                    "message": (
                        f"{exc.month} 第 {exc.report_version} 版月报封版失败："
                        f"缺少 {len(exc.missing)} 个院区文件，已保留上次封版结果"
                    ),
                    "month": exc.month,
                    "report_version": exc.report_version,
                    "missing": exc.missing,
                    "current_report_version": exc.current_report_version,
                },
            )
        except store_mod.ReportVersionConflict as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "version_conflict",
                    "message": str(exc),
                    "month": exc.month,
                    "requested_report_version": exc.requested,
                    "current_report_version": exc.current,
                },
            )

    # -- 下游查询 -----------------------------------------------------------

    @app.get(
        "/api/v1/monthly-reports/{month}",
        response_model=MonthlyReportResponse,
        responses={404: {"model": NotFoundResponse, "description": "该月尚未封版"}},
        tags=["查询"],
        summary="下游按月查询当前版号与整表",
    )
    def get_monthly_report(month: MonthPath):
        report = store.get_report(month)
        if report is None:
            return JSONResponse(
                status_code=404,
                content={
                    "status": "not_found",
                    "message": f"{month} 尚未封版，暂无可用月报",
                },
            )
        return report

    return app


app = create_app()
