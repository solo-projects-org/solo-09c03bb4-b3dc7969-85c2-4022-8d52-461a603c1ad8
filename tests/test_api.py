"""FastAPI 层测试：状态码、响应结构、校验规则与并发封版。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

MONTH = "2026-10"
FILE_URL = "/api/v1/report-files/{month}/{campus}/{version}"
FINALIZE_URL = "/api/v1/monthly-reports/finalize"
REPORT_URL = "/api/v1/monthly-reports/{month}"

EAST_ROWS = {
    "rows": [
        {"township": "城关镇", "age_group": "0-17", "count": 3},
        {"township": "城关镇", "age_group": "18-59", "count": 12},
        {"township": "桃源镇", "age_group": "60+", "count": 2},
    ]
}
WEST_ROWS = {
    "rows": [
        {"township": "城关镇", "age_group": "0-17", "count": 1},
        {"township": "青溪镇", "age_group": "18-59", "count": 7},
    ]
}
EMPTY_ROWS = {"rows": []}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def put_file(client, campus, version, payload, month=MONTH):
    return client.put(
        FILE_URL.format(month=month, campus=campus, version=version), json=payload
    )


def finalize(client, version, files, month=MONTH):
    return client.post(
        FINALIZE_URL,
        json={"month": month, "report_version": version, "files": files},
    )


def all_files(east=1, west=1, lake=1):
    return [
        {"campus": "东城院区", "file_version": east},
        {"campus": "西城院区", "file_version": west},
        {"campus": "滨湖院区", "file_version": lake},
    ]


def upload_all(client, month=MONTH):
    put_file(client, "东城院区", 1, EAST_ROWS, month)
    put_file(client, "西城院区", 1, WEST_ROWS, month)
    put_file(client, "滨湖院区", 1, EMPTY_ROWS, month)  # 零病例报空表


# -- 基础 -------------------------------------------------------------------


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "case-report-seal"}


def test_upload_received_then_idempotent(client):
    resp = put_file(client, "东城院区", 1, EAST_ROWS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert body["row_count"] == 3
    assert body["total_cases"] == 17

    again = put_file(client, "东城院区", 1, EAST_ROWS)
    assert again.status_code == 200
    assert again.json()["status"] == "already_received"
    assert again.json()["checksum"] == body["checksum"]


def test_upload_conflict_same_version_different_counts(client):
    put_file(client, "东城院区", 1, EAST_ROWS)
    changed = {"rows": [dict(EAST_ROWS["rows"][0], count=99)]}
    resp = put_file(client, "东城院区", 1, changed)
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "file_conflict"
    assert body["existing"]["total_cases"] == 17
    assert body["rejected"]["total_cases"] == 99
    # 已收数据未被覆盖
    stored = client.get(FILE_URL.format(month=MONTH, campus="东城院区", version=1))
    assert stored.json()["total_cases"] == 17


def test_upload_empty_table_accepted(client):
    resp = put_file(client, "滨湖院区", 1, EMPTY_ROWS)
    assert resp.status_code == 200
    assert resp.json()["row_count"] == 0
    assert resp.json()["total_cases"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"rows": [{"township": "城关镇", "age_group": "0-17", "count": -1}]},   # 负数
        {"rows": [{"township": "城关镇", "age_group": "0-17", "count": True}]},  # 布尔
        {"rows": [{"township": "城关镇", "age_group": "0-17", "count": 1.5}]},   # 小数
        {"rows": [{"township": "", "age_group": "0-17", "count": 1}]},           # 空乡镇
        {"rows": [{"township": "城关镇", "age_group": "0-17"}]},                 # 缺字段
        {"rows": [                                                              # 乡镇+年龄组重复
            {"township": "城关镇", "age_group": "0-17", "count": 1},
            {"township": "城关镇", "age_group": "0-17", "count": 2},
        ]},
    ],
)
def test_upload_validation_422(client, payload):
    assert put_file(client, "东城院区", 1, payload).status_code == 422


@pytest.mark.parametrize("bad_month", ["2026-13", "2026-00", "26-10", "2026-1"])
def test_month_format_422(client, bad_month):
    assert put_file(client, "东城院区", 1, EAST_ROWS, month=bad_month).status_code == 422


def test_month_with_slash_not_routed(client):
    # 含斜杠的月份无法匹配任何路由，直接 404
    assert put_file(client, "东城院区", 1, EAST_ROWS, month="2026/10").status_code == 404


def test_list_and_get_files(client):
    upload_all(client)
    listed = client.get(f"/api/v1/report-files/{MONTH}")
    assert listed.status_code == 200
    files = listed.json()["files"]
    assert {(f["campus"], f["file_version"]) for f in files} == {
        ("东城院区", 1), ("西城院区", 1), ("滨湖院区", 1),
    }
    one = client.get(FILE_URL.format(month=MONTH, campus="西城院区", version=1))
    assert one.status_code == 200
    assert one.json()["rows"] == WEST_ROWS["rows"]
    missing = client.get(FILE_URL.format(month=MONTH, campus="西城院区", version=9))
    assert missing.status_code == 404
    assert missing.json()["status"] == "not_found"


# -- 封版与查询 ---------------------------------------------------------------


def test_finalize_and_query_flow(client):
    upload_all(client)
    resp = finalize(client, 1, all_files())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "finalized"
    assert body["previous_report_version"] is None
    assert body["total_cases"] == 25
    assert body["rows"] == [
        {"township": "城关镇", "age_group": "0-17", "count": 4},
        {"township": "城关镇", "age_group": "18-59", "count": 12},
        {"township": "桃源镇", "age_group": "60+", "count": 2},
        {"township": "青溪镇", "age_group": "18-59", "count": 7},
    ]

    report = client.get(REPORT_URL.format(month=MONTH))
    assert report.status_code == 200
    assert report.json()["report_version"] == 1
    assert report.json()["rows"] == body["rows"]


def test_query_unsealed_month_404(client):
    resp = client.get(REPORT_URL.format(month="2026-11"))
    assert resp.status_code == 404
    assert resp.json()["status"] == "not_found"


def test_late_file_then_refinalize(client):
    """核心场景：迟到文件补齐前封版报缺件、下游看旧版；补齐后封版切换。"""
    upload_all(client)
    assert finalize(client, 1, all_files()).status_code == 200

    # 东城、西城报送第 2 版文件，西城文件迟到
    put_file(client, "东城院区", 2, {"rows": [
        {"township": "城关镇", "age_group": "0-17", "count": 30},
    ]})
    resp = finalize(client, 2, all_files(east=2, west=2, lake=1))
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "missing_files"
    assert body["missing"] == [{"campus": "西城院区", "file_version": 2}]
    assert body["current_report_version"] == 1

    # 新版未收齐，下游仍查到上一版
    report = client.get(REPORT_URL.format(month=MONTH)).json()
    assert report["report_version"] == 1
    assert report["total_cases"] == 25

    # 补齐迟到文件后再次封版：整表一次替换
    put_file(client, "西城院区", 2, {"rows": [
        {"township": "青溪镇", "age_group": "18-59", "count": 70},
    ]})
    resp = finalize(client, 2, all_files(east=2, west=2, lake=1))
    assert resp.status_code == 200
    assert resp.json()["previous_report_version"] == 1
    report = client.get(REPORT_URL.format(month=MONTH)).json()
    assert report["report_version"] == 2
    assert report["rows"] == [
        {"township": "城关镇", "age_group": "0-17", "count": 30},
        {"township": "青溪镇", "age_group": "18-59", "count": 70},
    ]


def test_finalize_version_must_increase(client):
    upload_all(client)
    assert finalize(client, 1, all_files()).status_code == 200
    for bad in (1, 0, -3):
        resp = finalize(client, bad, all_files())
        assert resp.status_code in (409, 422), (bad, resp.status_code)
        if resp.status_code == 409:
            assert resp.json()["status"] == "version_conflict"
            assert resp.json()["current_report_version"] == 1
    assert client.get(REPORT_URL.format(month=MONTH)).json()["report_version"] == 1


def test_finalize_validation_422(client):
    # 院区重复
    resp = finalize(client, 1, [
        {"campus": "东城院区", "file_version": 1},
        {"campus": "东城院区", "file_version": 2},
    ])
    assert resp.status_code == 422
    # 空应报列表
    assert finalize(client, 1, []).status_code == 422
    # 月份格式错误
    assert finalize(client, 1, all_files(), month="2026-13").status_code == 422
    # 文件版本号非法
    assert finalize(client, 1, [{"campus": "东城院区", "file_version": 0}]).status_code == 422


def test_concurrent_finalize_via_api(client):
    """经 HTTP 并发封版：最终版号为最大值，响应只有 200/409 两种。"""
    upload_all(client)
    versions = list(range(1, 16))
    results: list[tuple[int, int]] = []  # (version, status_code)
    barrier = threading.Barrier(len(versions))

    def attempt(v: int) -> None:
        barrier.wait()
        resp = finalize(client, v, all_files())
        results.append((v, resp.status_code))

    threads = [threading.Thread(target=attempt, args=(v,)) for v in versions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code in (200, 409) for _, code in results)
    assert any(code == 200 for _, code in results)
    report = client.get(REPORT_URL.format(month=MONTH)).json()
    assert report["report_version"] == max(versions)
    assert report["total_cases"] == 25


def test_swagger_docs_available(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/api/v1/monthly-reports/finalize" in spec["paths"]
    assert "/api/v1/monthly-reports/{month}" in spec["paths"]
