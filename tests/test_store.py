"""存储层测试：收件幂等、封版事务、版本单调与并发不回退。"""

from __future__ import annotations

import threading

import pytest

from app.store import (
    FileVersionConflict,
    MissingFiles,
    ReportStore,
    ReportVersionConflict,
)

MONTH = "2026-10"


@pytest.fixture()
def store(tmp_path):
    s = ReportStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def rows(*items: tuple[str, str, int]) -> list[dict]:
    return [{"township": t, "age_group": a, "count": c} for t, a, c in items]


# -- 收件 -------------------------------------------------------------------


def test_save_and_idempotent_resend(store):
    payload = rows(("城关镇", "0-17", 3), ("城关镇", "18-59", 12))
    first = store.save_file(MONTH, "东城院区", 1, payload)
    assert first["status"] == "received"
    assert first["row_count"] == 2
    assert first["total_cases"] == 15

    # 同版本、同计数（行顺序不同也算相同）：沿用已收数据
    again = store.save_file(MONTH, "东城院区", 1, list(reversed(payload)))
    assert again["status"] == "already_received"
    assert again["received_at"] == first["received_at"]
    assert again["checksum"] == first["checksum"]


def test_resend_same_version_different_counts_rejected(store):
    store.save_file(MONTH, "东城院区", 1, rows(("城关镇", "0-17", 3)))
    with pytest.raises(FileVersionConflict) as exc_info:
        store.save_file(MONTH, "东城院区", 1, rows(("城关镇", "0-17", 4)))
    assert exc_info.value.existing["total_cases"] == 3
    assert exc_info.value.rejected["total_cases"] == 4
    # 已收数据不被覆盖
    kept = store.get_file(MONTH, "东城院区", 1)
    assert kept is not None and kept["total_cases"] == 3


def test_zero_case_empty_table(store):
    result = store.save_file(MONTH, "滨湖院区", 1, [])
    assert result["status"] == "received"
    assert result["row_count"] == 0
    assert result["total_cases"] == 0
    # 空表重传同样幂等
    assert store.save_file(MONTH, "滨湖院区", 1, [])["status"] == "already_received"


def test_multiple_versions_of_same_campus_coexist(store):
    store.save_file(MONTH, "东城院区", 1, rows(("城关镇", "0-17", 3)))
    store.save_file(MONTH, "东城院区", 2, rows(("城关镇", "0-17", 9)))
    listed = store.list_files(MONTH)
    assert [(f["campus"], f["file_version"]) for f in listed] == [
        ("东城院区", 1),
        ("东城院区", 2),
    ]


# -- 封版 -------------------------------------------------------------------


def _upload_all(store, month=MONTH):
    store.save_file(month, "东城院区", 1, rows(
        ("城关镇", "0-17", 3), ("城关镇", "18-59", 12), ("桃源镇", "60+", 2),
    ))
    store.save_file(month, "西城院区", 1, rows(
        ("城关镇", "0-17", 1), ("青溪镇", "18-59", 7),
    ))
    store.save_file(month, "滨湖院区", 1, [])  # 零病例报空表


def _finalize(store, version, month=MONTH, east=1, west=1, lake=1):
    return store.finalize(
        month,
        version,
        [
            {"campus": "东城院区", "file_version": east},
            {"campus": "西城院区", "file_version": west},
            {"campus": "滨湖院区", "file_version": lake},
        ],
    )


def test_finalize_aggregates_and_replaces_whole_table(store):
    _upload_all(store)
    result = _finalize(store, 1)
    assert result["status"] == "finalized"
    assert result["previous_report_version"] is None
    # 跨院区按乡镇+年龄组加总，整表排序输出
    assert result["rows"] == [
        {"township": "城关镇", "age_group": "0-17", "count": 4},
        {"township": "城关镇", "age_group": "18-59", "count": 12},
        {"township": "桃源镇", "age_group": "60+", "count": 2},
        {"township": "青溪镇", "age_group": "18-59", "count": 7},
    ]
    assert result["total_cases"] == 25
    report = store.get_report(MONTH)
    assert report is not None
    assert report["report_version"] == 1
    assert report["rows"] == result["rows"]


def test_finalize_missing_files_keeps_previous_report(store):
    _upload_all(store)
    _finalize(store, 1)

    # 东城院区报送第 2 版，但西城院区第 2 版迟到：封版缺件
    store.save_file(MONTH, "东城院区", 2, rows(("城关镇", "0-17", 30)))
    with pytest.raises(MissingFiles) as exc_info:
        _finalize(store, 2, east=2, west=2, lake=1)
    assert exc_info.value.missing == [{"campus": "西城院区", "file_version": 2}]
    assert exc_info.value.current_report_version == 1

    # 缺件期间下游仍查到上一版
    report = store.get_report(MONTH)
    assert report is not None
    assert report["report_version"] == 1
    assert report["total_cases"] == 25

    # 迟到文件补齐后再次封版成功，整表一次替换
    store.save_file(MONTH, "西城院区", 2, rows(("青溪镇", "18-59", 70)))
    result = _finalize(store, 2, east=2, west=2, lake=1)
    assert result["previous_report_version"] == 1
    assert result["rows"] == [
        {"township": "城关镇", "age_group": "0-17", "count": 30},
        {"township": "青溪镇", "age_group": "18-59", "count": 70},
    ]
    assert store.get_report(MONTH)["report_version"] == 2  # type: ignore[index]


def test_finalize_never_sealed_month_missing(store):
    store.save_file(MONTH, "东城院区", 1, rows(("城关镇", "0-17", 3)))
    with pytest.raises(MissingFiles) as exc_info:
        _finalize(store, 1)
    assert {m["campus"] for m in exc_info.value.missing} == {"西城院区", "滨湖院区"}
    assert exc_info.value.current_report_version is None
    assert store.get_report(MONTH) is None


def test_report_version_must_increase(store):
    _upload_all(store)
    _finalize(store, 2)
    # 同版号与更低版号都被拒绝
    for bad in (2, 1):
        with pytest.raises(ReportVersionConflict) as exc_info:
            _finalize(store, bad)
        assert exc_info.value.current == 2
        assert exc_info.value.requested == bad
    assert store.get_report(MONTH)["report_version"] == 2  # type: ignore[index]


def test_concurrent_finalize_never_rolls_back(store):
    """并发封版：多线程同时提交不同版号，最终版号必为最大值。"""
    _upload_all(store)
    versions = list(range(1, 21))
    outcomes: list[tuple[str, int]] = []
    barrier = threading.Barrier(len(versions))

    def attempt(v: int) -> None:
        barrier.wait()  # 尽量同时进入
        try:
            _finalize(store, v)
            outcomes.append(("finalized", v))
        except ReportVersionConflict as exc:
            outcomes.append(("rejected", exc.requested))

    threads = [threading.Thread(target=attempt, args=(v,)) for v in versions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    finalized = [v for status, v in outcomes if status == "finalized"]
    rejected = [v for status, v in outcomes if status == "rejected"]
    # 每个版号恰好产生一个结果；只有部分版号能封版成功
    assert sorted(finalized + rejected) == versions
    assert finalized
    # 成功的版号序列必然严格递增，且最终生效的是全局最大版号
    assert finalized == sorted(finalized)
    report = store.get_report(MONTH)
    assert report is not None
    assert report["report_version"] == max(versions)


def test_finalize_is_atomic_on_failure(store):
    """缺件封版失败不得留下半成品状态。"""
    _upload_all(store)
    _finalize(store, 1)
    with pytest.raises(MissingFiles):
        store.finalize(
            MONTH,
            2,
            [{"campus": "不存在的院区", "file_version": 9}],
        )
    report = store.get_report(MONTH)
    assert report is not None and report["report_version"] == 1
