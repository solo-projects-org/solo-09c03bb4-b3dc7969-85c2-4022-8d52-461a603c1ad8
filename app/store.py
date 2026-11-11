"""SQLite 持久化层：院区月报文件收件、整表封版与下游查询。

存储模型
--------

* ``campus_files``：院区报送的原始文件，主键为 ``(month, campus,
  file_version)``。同一主键重传时比对规范化内容的 SHA-256：一致则沿用
  已收数据（幂等成功），不一致则拒绝，已收数据不被覆盖。
* ``monthly_reports``：封版后的整表月报，主键为 ``month``。封版在
  ``BEGIN IMMEDIATE`` 事务内完成「版本检查 -> 缺件检查 -> 加总 ->
  整表替换」，因此并发封版彼此串行，版号只会单调上升，不会回退。

规范化：文件行按 ``(township, age_group)`` 排序后以紧凑 JSON 序列化，
保证「计数相同」的判定与行顺序、空白无关。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS campus_files (
    month        TEXT NOT NULL,
    campus       TEXT NOT NULL,
    file_version INTEGER NOT NULL,
    payload      TEXT NOT NULL,   -- 规范化后的行 JSON（按乡镇、年龄组排序）
    checksum     TEXT NOT NULL,   -- payload 的 SHA-256，用于同版本重传比对
    row_count    INTEGER NOT NULL,
    total_cases  INTEGER NOT NULL,
    received_at  TEXT NOT NULL,
    PRIMARY KEY (month, campus, file_version)
);

CREATE TABLE IF NOT EXISTS monthly_reports (
    month          TEXT PRIMARY KEY,
    report_version INTEGER NOT NULL,
    payload        TEXT NOT NULL,  -- 加总后的整表 JSON
    row_count      INTEGER NOT NULL,
    total_cases    INTEGER NOT NULL,
    finalized_at   TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonicalize_rows(rows: list[dict]) -> list[dict]:
    """把报送行整理为规范化形式：按（乡镇、年龄组）排序。"""
    return sorted(rows, key=lambda r: (r["township"], r["age_group"]))


def _dumps(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _checksum(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def aggregate_rows(all_rows: list[dict]) -> list[dict]:
    """按（乡镇、年龄组）加总病例数，返回排序后的整表行。"""
    totals: dict[tuple[str, str], int] = {}
    for row in all_rows:
        key = (row["township"], row["age_group"])
        totals[key] = totals.get(key, 0) + row["count"]
    return [
        {"township": township, "age_group": age_group, "count": count}
        for (township, age_group), count in sorted(totals.items())
    ]


# ---------------------------------------------------------------------------
# 业务异常（由 API 层映射为 409）
# ---------------------------------------------------------------------------


class StoreError(Exception):
    """持久化层业务冲突的基类。"""


class FileVersionConflict(StoreError):
    """同一 (month, campus, file_version) 重传了计数不同的内容。"""

    def __init__(self, month: str, campus: str, file_version: int,
                 existing: dict, rejected: dict):
        super().__init__(
            f"{month} {campus} 文件版本 {file_version} 已存在且计数不同，已拒绝覆盖"
        )
        self.month = month
        self.campus = campus
        self.file_version = file_version
        self.existing = existing
        self.rejected = rejected


class MissingFiles(StoreError):
    """封版时指定的院区文件版本尚未收齐。"""

    def __init__(self, month: str, report_version: int,
                 missing: list[dict], current_report_version: int | None):
        super().__init__(f"{month} 封版缺件：{missing}")
        self.month = month
        self.report_version = report_version
        self.missing = missing
        self.current_report_version = current_report_version


class ReportVersionConflict(StoreError):
    """封版版号未高于当前版号（含并发场景下被更高版号抢先）。"""

    def __init__(self, month: str, requested: int, current: int):
        super().__init__(
            f"{month} 当前月报版号为 {current}，只能切换到更高版号，"
            f"本次提交的 {requested} 已被拒绝"
        )
        self.month = month
        self.requested = requested
        self.current = current


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class ReportStore:
    """线程安全的 SQLite 存储。

    单连接 + 进程内可重入锁：所有读写串行执行；写事务使用
    ``BEGIN IMMEDIATE``，即使未来改为多连接/多进程部署，SQLite 层面
    依然保证封版事务互斥。
    """

    def __init__(self, db_path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 收件 ---------------------------------------------------------------

    def save_file(
        self,
        month: str,
        campus: str,
        file_version: int,
        rows: list[dict],
    ) -> dict:
        """保存院区报送文件；同版本重传做幂等/冲突判定。"""
        canonical = canonicalize_rows(rows)
        payload = _dumps(canonical)
        checksum = _checksum(payload)
        total_cases = sum(r["count"] for r in canonical)
        summary = {
            "row_count": len(canonical),
            "total_cases": total_cases,
            "checksum": checksum,
        }
        with self._lock:
            existing = self._conn.execute(
                "SELECT payload, checksum, row_count, total_cases, received_at "
                "FROM campus_files WHERE month=? AND campus=? AND file_version=?",
                (month, campus, file_version),
            ).fetchone()
            if existing is not None:
                if existing["checksum"] == checksum:
                    # 同版本、同计数：沿用已收数据，幂等返回
                    return {
                        "status": "already_received",
                        "month": month,
                        "campus": campus,
                        "file_version": file_version,
                        "row_count": existing["row_count"],
                        "total_cases": existing["total_cases"],
                        "checksum": existing["checksum"],
                        "received_at": existing["received_at"],
                    }
                raise FileVersionConflict(
                    month,
                    campus,
                    file_version,
                    existing={
                        "row_count": existing["row_count"],
                        "total_cases": existing["total_cases"],
                        "checksum": existing["checksum"],
                    },
                    rejected=summary,
                )
            received_at = _utc_now()
            self._conn.execute(
                "INSERT INTO campus_files "
                "(month, campus, file_version, payload, checksum, row_count, "
                " total_cases, received_at) VALUES (?,?,?,?,?,?,?,?)",
                (month, campus, file_version, payload, checksum,
                 len(canonical), total_cases, received_at),
            )
            return {
                "status": "received",
                "month": month,
                "campus": campus,
                "file_version": file_version,
                "row_count": len(canonical),
                "total_cases": total_cases,
                "checksum": checksum,
                "received_at": received_at,
            }

    def get_file(self, month: str, campus: str, file_version: int) -> dict | None:
        """读取单个已收文件；不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, checksum, row_count, total_cases, received_at "
                "FROM campus_files WHERE month=? AND campus=? AND file_version=?",
                (month, campus, file_version),
            ).fetchone()
        if row is None:
            return None
        return {
            "month": month,
            "campus": campus,
            "file_version": file_version,
            "row_count": row["row_count"],
            "total_cases": row["total_cases"],
            "checksum": row["checksum"],
            "received_at": row["received_at"],
            "rows": json.loads(row["payload"]),
        }

    def list_files(self, month: str) -> list[dict]:
        """列出某统计月已收到的全部文件（按院区、文件版本排序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT campus, file_version, row_count, total_cases, checksum, "
                "received_at FROM campus_files WHERE month=? "
                "ORDER BY campus, file_version",
                (month,),
            ).fetchall()
        return [
            {
                "campus": r["campus"],
                "file_version": r["file_version"],
                "row_count": r["row_count"],
                "total_cases": r["total_cases"],
                "checksum": r["checksum"],
                "received_at": r["received_at"],
            }
            for r in rows
        ]

    # -- 封版 ---------------------------------------------------------------

    def finalize(self, month: str, report_version: int, files: list[dict]) -> dict:
        """封版：收齐指定文件版本后整表替换月报。

        整个「版本检查 -> 缺件检查 -> 加总 -> 替换」在 BEGIN IMMEDIATE
        事务内完成；任何一步失败都回滚，已封版结果保持不变。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._conn.execute(
                    "SELECT report_version FROM monthly_reports WHERE month=?",
                    (month,),
                ).fetchone()
                current_version = current["report_version"] if current else None

                # 月报只能切换到更高版号；并发下后进入事务者会看到
                # 已提交的新版号，从而被拒绝，不会回退。
                if current_version is not None and report_version <= current_version:
                    raise ReportVersionConflict(month, report_version, current_version)

                missing: list[dict] = []
                all_rows: list[dict] = []
                for ref in files:
                    row = self._conn.execute(
                        "SELECT payload FROM campus_files "
                        "WHERE month=? AND campus=? AND file_version=?",
                        (month, ref["campus"], ref["file_version"]),
                    ).fetchone()
                    if row is None:
                        missing.append(
                            {"campus": ref["campus"], "file_version": ref["file_version"]}
                        )
                    else:
                        all_rows.extend(json.loads(row["payload"]))

                if missing:
                    raise MissingFiles(month, report_version, missing, current_version)

                table = aggregate_rows(all_rows)
                payload = _dumps(table)
                total_cases = sum(r["count"] for r in table)
                finalized_at = _utc_now()
                self._conn.execute(
                    "INSERT INTO monthly_reports "
                    "(month, report_version, payload, row_count, total_cases, "
                    " finalized_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(month) DO UPDATE SET "
                    "report_version=excluded.report_version, "
                    "payload=excluded.payload, "
                    "row_count=excluded.row_count, "
                    "total_cases=excluded.total_cases, "
                    "finalized_at=excluded.finalized_at",
                    (month, report_version, payload, len(table), total_cases,
                     finalized_at),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

        return {
            "status": "finalized",
            "month": month,
            "report_version": report_version,
            "previous_report_version": current_version,
            "applied_files": [
                {"campus": ref["campus"], "file_version": ref["file_version"]}
                for ref in files
            ],
            "row_count": len(table),
            "total_cases": total_cases,
            "finalized_at": finalized_at,
            "rows": table,
        }

    # -- 下游查询 -------------------------------------------------------------

    def get_report(self, month: str) -> dict | None:
        """查询某月当前封版结果；未封版返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT report_version, payload, row_count, total_cases, "
                "finalized_at FROM monthly_reports WHERE month=?",
                (month,),
            ).fetchone()
        if row is None:
            return None
        return {
            "month": month,
            "report_version": row["report_version"],
            "finalized_at": row["finalized_at"],
            "row_count": row["row_count"],
            "total_cases": row["total_cases"],
            "rows": json.loads(row["payload"]),
        }
