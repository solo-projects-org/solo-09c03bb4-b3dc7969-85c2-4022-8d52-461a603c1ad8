"""请求/响应 Pydantic 模型与 Swagger 示例。

演示场景（与 README 的 Swagger 演练一致）：统计月 2026-10，应报院区为
东城院区、西城院区、滨湖院区；乡镇示例为城关镇/桃源镇/青溪镇，年龄组
为 0-17 / 18-59 / 60+。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
MONTH_DESC = "统计月，格式 YYYY-MM"
MAX_FILE_VERSION = 1_000_000
MAX_REPORT_VERSION = 1_000_000

# ---------------------------------------------------------------------------
# Swagger 示例数据
# ---------------------------------------------------------------------------

# 东城院区 2026-10 第 1 版文件：按病例归属乡镇 + 年龄组的全量计数。
EX_UPLOAD_ROWS: dict = {
    "rows": [
        {"township": "城关镇", "age_group": "0-17", "count": 3},
        {"township": "城关镇", "age_group": "18-59", "count": 12},
        {"township": "城关镇", "age_group": "60+", "count": 8},
        {"township": "桃源镇", "age_group": "18-59", "count": 5},
        {"township": "桃源镇", "age_group": "60+", "count": 2},
    ]
}

# 零病例院区也必须报送：空表即 rows 为空数组。
EX_UPLOAD_EMPTY: dict = {"rows": []}

# 封版请求：指定月报版号和每个应报院区的文件版本。
EX_FINALIZE: dict = {
    "month": "2026-10",
    "report_version": 1,
    "files": [
        {"campus": "东城院区", "file_version": 1},
        {"campus": "西城院区", "file_version": 1},
        {"campus": "滨湖院区", "file_version": 1},
    ],
}


# ---------------------------------------------------------------------------
# 公共行模型
# ---------------------------------------------------------------------------


def _reject_bool(value: int, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 不能是布尔值，须为非负整数")
    return value


class ReportRow(BaseModel):
    """一行病例计数：病例归属乡镇 + 年龄组 + 病例数。"""

    township: str = Field(..., min_length=1, max_length=50, description="病例归属乡镇")
    age_group: str = Field(..., min_length=1, max_length=20, description="年龄组，如 0-17、18-59、60+")
    count: StrictInt = Field(..., ge=0, le=999_999_999, description="病例数（非负整数）")

    @field_validator("count")
    @classmethod
    def _count_not_bool(cls, value: int) -> int:
        return _reject_bool(value, "count")


class CampusFileRef(BaseModel):
    """封版时指定的一个应报院区文件版本。"""

    campus: str = Field(..., min_length=1, max_length=50, description="院区名称")
    file_version: int = Field(
        ..., ge=1, le=MAX_FILE_VERSION, description="该院区应纳入封版的文件版本号"
    )


# ---------------------------------------------------------------------------
# 报送（院区 -> 总院）
# ---------------------------------------------------------------------------


class ReportFilePayload(BaseModel):
    """院区报送的当月全量计数文件；零病例时报空表（rows 为 []）。"""

    rows: list[ReportRow] = Field(
        ...,
        max_length=10_000,
        description="当月全量计数行，每行含乡镇、年龄组、病例数；零病例报送空数组",
    )

    @model_validator(mode="after")
    def _check_duplicate_keys(self) -> "ReportFilePayload":
        seen: set[tuple[str, str]] = set()
        for row in self.rows:
            key = (row.township, row.age_group)
            if key in seen:
                raise ValueError(
                    f"同一文件内乡镇+年龄组重复：{row.township} / {row.age_group}"
                )
            seen.add(key)
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"summary": "正常报送：5 行计数", "value": EX_UPLOAD_ROWS},
                {"summary": "零病例：报送空表", "value": EX_UPLOAD_EMPTY},
            ]
        }
    }


class FileSummary(BaseModel):
    row_count: int
    total_cases: int
    checksum: str


class FileReceivedResponse(BaseModel):
    status: Literal["received", "already_received"] = Field(
        ..., description="received=首次接收；already_received=同版本同计数重传，沿用已收数据"
    )
    month: str
    campus: str
    file_version: int
    row_count: int
    total_cases: int
    checksum: str
    received_at: str


class FileConflictResponse(BaseModel):
    status: Literal["file_conflict"]
    message: str
    month: str
    campus: str
    file_version: int
    existing: FileSummary = Field(..., description="已收数据的摘要（保持不变）")
    rejected: FileSummary = Field(..., description="本次重传被驳回的内容摘要")


class StoredFileResponse(BaseModel):
    month: str
    campus: str
    file_version: int
    row_count: int
    total_cases: int
    checksum: str
    received_at: str
    rows: list[ReportRow]


class FileListItem(BaseModel):
    campus: str
    file_version: int
    row_count: int
    total_cases: int
    checksum: str
    received_at: str


class FileListResponse(BaseModel):
    month: str
    files: list[FileListItem]


# ---------------------------------------------------------------------------
# 封版（工程师）
# ---------------------------------------------------------------------------


class FinalizeRequest(BaseModel):
    """封版请求：月份、月报版号、每个应报院区的指定文件版本。"""

    month: str = Field(..., pattern=MONTH_PATTERN, description=MONTH_DESC)
    report_version: int = Field(
        ...,
        ge=1,
        le=MAX_REPORT_VERSION,
        description="月报版号；必须高于当前已封版号，只能单调上升",
    )
    files: list[CampusFileRef] = Field(
        ...,
        min_length=1,
        max_length=200,
        description="每个应报院区及其指定文件版本；全部收齐才能封版",
    )

    @model_validator(mode="after")
    def _check_duplicate_campus(self) -> "FinalizeRequest":
        campuses = [f.campus for f in self.files]
        if len(set(campuses)) != len(campuses):
            raise ValueError("files 中院区重复，每个应报院区只能出现一次")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"summary": "封版 2026-10 第 1 版月报（三个应报院区）", "value": EX_FINALIZE},
            ]
        }
    }


class FinalizedResponse(BaseModel):
    status: Literal["finalized"]
    month: str
    report_version: int
    previous_report_version: int | None = Field(
        ..., description="被替换的旧版号；首次封版为 null"
    )
    applied_files: list[CampusFileRef] = Field(..., description="本次封版实际采用的院区文件版本")
    row_count: int
    total_cases: int
    finalized_at: str
    rows: list[ReportRow] = Field(..., description="按乡镇+年龄组加总后的整表")


class MissingFilesResponse(BaseModel):
    status: Literal["missing_files"]
    message: str
    month: str
    report_version: int
    missing: list[CampusFileRef] = Field(..., description="尚未收到的院区文件版本")
    current_report_version: int | None = Field(
        ..., description="当前仍生效的封版版号（缺件封版不会改动它）；从未封版为 null"
    )


class VersionConflictResponse(BaseModel):
    status: Literal["version_conflict"]
    message: str
    month: str
    requested_report_version: int
    current_report_version: int


# ---------------------------------------------------------------------------
# 下游查询
# ---------------------------------------------------------------------------


class MonthlyReportResponse(BaseModel):
    month: str
    report_version: int = Field(..., description="当前生效的月报版号")
    finalized_at: str
    row_count: int
    total_cases: int
    rows: list[ReportRow] = Field(..., description="整表：按乡镇+年龄组加总后的计数")


class NotFoundResponse(BaseModel):
    status: Literal["not_found"]
    message: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
