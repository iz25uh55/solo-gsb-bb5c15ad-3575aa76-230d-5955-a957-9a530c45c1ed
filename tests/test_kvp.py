"""GET /jobs/{id}/series/{series_uid}/kvp：逐帧 KVP 分布、优先级回退与冲突明细。"""
from __future__ import annotations

import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, EnhancedCTImageStorage, ExplicitVRLittleEndian

from app.crypto import derive_uid
from tests.sample_data import _write_bytes

SECRET = "kvp-secret-123456"

STUDY = "3.1"
SERIES = "2.1"


# --------------------------------------------------------------------------- #
# 合成 DICOM
# --------------------------------------------------------------------------- #
def _ct_exposure_item(kvp) -> Dataset:
    item = Dataset()
    item.KVP = kvp
    return item


def _functional_group(kvp) -> Dataset:
    fg = Dataset()
    fg.CTExposureSequence = [_ct_exposure_item(kvp)]
    return fg


def _image_bytes(
    sop_uid: str,
    *,
    series_uid: str = SERIES,
    study_uid: str = STUDY,
    kvp=None,
    number_of_frames=None,
    per_frame_kvps=(),
    shared_kvp=None,
    drop: tuple[str, ...] = (),
):
    """构造 CT 实例；per_frame_kvps 给出时写入逐帧功能组，shared_kvp 写共享功能组。"""
    file_meta = FileMetaDataset()
    enhanced = bool(per_frame_kvps) or shared_kvp is not None
    sop_class = EnhancedCTImageStorage if enhanced else CTImageStorage
    file_meta.MediaStorageSOPClassUID = sop_class
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.Modality = "CT"
    if kvp is not None:
        ds.KVP = kvp
    if number_of_frames is not None:
        ds.NumberOfFrames = number_of_frames
    if per_frame_kvps:
        ds.PerFrameFunctionalGroupsSequence = [
            _functional_group(value) for value in per_frame_kvps
        ]
    if shared_kvp is not None:
        ds.SharedFunctionalGroupsSequence = [_functional_group(shared_kvp)]
    for keyword in drop:
        if keyword in ds:
            del ds[keyword]
    return _write_bytes(ds)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in files.items():
            zf.writestr(name, blob)
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DICOM_ANON_WORK_DIR", str(tmp_path / "jobs"))
    import importlib
    import app.config
    import app.jobs
    importlib.reload(app.config)
    importlib.reload(app.jobs)
    import app.main
    importlib.reload(app.main)
    with TestClient(app.main.app) as c:
        yield c


def _upload(client, files: dict[str, bytes]):
    r = client.post(
        "/jobs",
        files={"file": ("studies.zip", _zip_bytes(files), "application/zip")},
        data={"secret": SECRET, "shift_dates": "false"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _kvp(client, job_id: str, series_uid: str = SERIES):
    anon_series = derive_uid(SECRET, series_uid)
    return client.get(f"/jobs/{job_id}/series/{anon_series}/kvp")


def _anon(original: str) -> str:
    return derive_uid(SECRET, original)


# --------------------------------------------------------------------------- #
# 成功路径：顶层 KVP / 分布 / 缺失
# --------------------------------------------------------------------------- #
def test_kvp_single_frame_top_level(client):
    files = {
        "a.dcm": _image_bytes("1.1.1", kvp=120),
        "b.dcm": _image_bytes("1.1.2", kvp=120),
        "c.dcm": _image_bytes("1.1.3", kvp=80),
    }
    job = _upload(client, files)
    r = _kvp(client, job["id"])
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["series_instance_uid"] == _anon(SERIES)
    assert data["instance_count"] == 3
    assert data["total_frames"] == 3
    assert data["missing_kvp_frames"] == 0
    assert data["mixed_kvp"] is True
    assert data["voltage_distribution"] == [
        {"kvp": 80.0, "frame_count": 1},
        {"kvp": 120.0, "frame_count": 2},
    ]
    # 实例明细按匿名 SOP UID 字典序排列
    sop_uids = [item["sop_instance_uid"] for item in data["instances"]]
    assert sop_uids == sorted(sop_uids)
    by_uid = {item["sop_instance_uid"]: item for item in data["instances"]}
    assert by_uid[_anon("1.1.1")]["frame_kvps"] == [120.0]
    assert by_uid[_anon("1.1.3")]["frame_kvps"] == [80.0]
    assert all(item["kvp_source"] == "top_level" for item in data["instances"])
    assert all(item["number_of_frames"] == 1 for item in data["instances"])


def test_kvp_uniform_voltage_not_mixed(client):
    files = {
        "a.dcm": _image_bytes("1.1.1", kvp=100),
        "b.dcm": _image_bytes("1.1.2", kvp=100),
    }
    job = _upload(client, files)
    data = _kvp(client, job["id"]).json()
    assert data["mixed_kvp"] is False
    assert data["voltage_distribution"] == [
        {"kvp": 100.0, "frame_count": 2}
    ]


def test_kvp_missing_top_level_counted(client):
    files = {
        "a.dcm": _image_bytes("1.1.1", kvp=120),
        "b.dcm": _image_bytes("1.1.2"),  # 无 KVP
    }
    job = _upload(client, files)
    data = _kvp(client, job["id"]).json()
    assert data["total_frames"] == 2
    assert data["missing_kvp_frames"] == 1
    assert data["voltage_distribution"] == [
        {"kvp": 120.0, "frame_count": 1}
    ]
    by_uid = {item["sop_instance_uid"]: item for item in data["instances"]}
    assert by_uid[_anon("1.1.2")]["frame_kvps"] == [None]
    assert by_uid[_anon("1.1.2")]["kvp_source"] == "none"
    assert by_uid[_anon("1.1.2")]["missing_kvp_frames"] == 1


def test_kvp_unparseable_value_counted_as_missing(client):
    # KVP 为 VM>1 的畸形多值（KVP 按标准 VM=1）：无法解析 -> 计入缺失
    files = {
        "a.dcm": _image_bytes("1.1.1", kvp=120),
        "b.dcm": _image_bytes("1.1.2", kvp=[120, 130]),
    }
    job = _upload(client, files)
    data = _kvp(client, job["id"]).json()
    assert data["missing_kvp_frames"] == 1
    assert data["voltage_distribution"] == [
        {"kvp": 120.0, "frame_count": 1}
    ]


def test_kvp_number_of_frames_one_is_single_frame(client):
    files = {"a.dcm": _image_bytes("1.1.1", kvp=120, number_of_frames=1)}
    job = _upload(client, files)
    data = _kvp(client, job["id"]).json()
    assert data["total_frames"] == 1
    assert data["instances"][0]["number_of_frames"] == 1
    assert data["instances"][0]["frame_kvps"] == [120.0]


def test_kvp_only_requested_series_analyzed(client):
    files = {
        "a.dcm": _image_bytes("1.1.1", kvp=120),
        "other.dcm": _image_bytes("9.9.1", series_uid="2.9", kvp=80),
    }
    job = _upload(client, files)
    data = _kvp(client, job["id"]).json()
    assert data["instance_count"] == 1
    assert data["voltage_distribution"] == [
        {"kvp": 120.0, "frame_count": 1}
    ]


# --------------------------------------------------------------------------- #
# 成功路径：多帧增强 CT 与优先级回退
# --------------------------------------------------------------------------- #
def test_kvp_per_frame_sequence(client):
    files = {
        "mf.dcm": _image_bytes(
            "1.1.1", number_of_frames=3, per_frame_kvps=[80, 120, 120]
        ),
    }
    job = _upload(client, files)
    r = _kvp(client, job["id"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_frames"] == 3
    assert data["mixed_kvp"] is True
    assert data["voltage_distribution"] == [
        {"kvp": 80.0, "frame_count": 1},
        {"kvp": 120.0, "frame_count": 2},
    ]
    item = data["instances"][0]
    assert item["number_of_frames"] == 3
    assert item["frame_kvps"] == [80.0, 120.0, 120.0]
    assert item["kvp_source"] == "per_frame"
    assert item["missing_kvp_frames"] == 0


def test_kvp_shared_functional_group_fallback(client):
    files = {
        "mf.dcm": _image_bytes(
            "1.1.1", number_of_frames=4, shared_kvp=100
        ),
    }
    job = _upload(client, files)
    data = _kvp(client, job["id"]).json()
    assert data["total_frames"] == 4
    assert data["voltage_distribution"] == [
        {"kvp": 100.0, "frame_count": 4}
    ]
    assert data["instances"][0]["kvp_source"] == "shared"
    assert data["instances"][0]["frame_kvps"] == [100.0] * 4


def test_kvp_per_frame_overrides_shared_and_top_level(client):
    # 三层都有值：逐帧功能组优先
    files = {
        "mf.dcm": _image_bytes(
            "1.1.1",
            kvp=140,
            number_of_frames=2,
            per_frame_kvps=[80, 100],
            shared_kvp=120,
        ),
    }
    job = _upload(client, files)
    data = _kvp(client, job["id"]).json()
    assert data["instances"][0]["frame_kvps"] == [80.0, 100.0]
    assert data["instances"][0]["kvp_source"] == "per_frame"


def test_kvp_shared_overrides_top_level(client):
    files = {
        "mf.dcm": _image_bytes(
            "1.1.1", kvp=140, number_of_frames=2, shared_kvp=120
        ),
    }
    job = _upload(client, files)
    data = _kvp(client, job["id"]).json()
    assert data["instances"][0]["frame_kvps"] == [120.0, 120.0]
    assert data["instances"][0]["kvp_source"] == "shared"


def test_kvp_per_frame_gap_falls_back_to_shared(client):
    # 第 2 帧的逐帧项没有 CTExposureSequence：该帧回退到共享功能组
    fg_with = _functional_group(80)
    fg_without = Dataset()  # 无 CTExposureSequence
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = EnhancedCTImageStorage
    file_meta.MediaStorageSOPInstanceUID = "1.1.1"
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = EnhancedCTImageStorage
    ds.SOPInstanceUID = "1.1.1"
    ds.StudyInstanceUID = STUDY
    ds.SeriesInstanceUID = SERIES
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.Modality = "CT"
    ds.NumberOfFrames = 2
    ds.PerFrameFunctionalGroupsSequence = [fg_with, fg_without]
    ds.SharedFunctionalGroupsSequence = [_functional_group(120)]

    job = _upload(client, {"mf.dcm": _write_bytes(ds)})
    data = _kvp(client, job["id"]).json()
    item = data["instances"][0]
    assert item["frame_kvps"] == [80.0, 120.0]
    assert item["kvp_source"] == "mixed"
    assert data["voltage_distribution"] == [
        {"kvp": 80.0, "frame_count": 1},
        {"kvp": 120.0, "frame_count": 1},
    ]


def test_kvp_multiframe_without_functional_groups_uses_top_level(client):
    # 经典多帧 CT：无功能组，顶层 KVP 应用于全部帧
    files = {
        "mf.dcm": _image_bytes("1.1.1", kvp=110, number_of_frames=3),
    }
    job = _upload(client, files)
    data = _kvp(client, job["id"]).json()
    assert data["total_frames"] == 3
    assert data["instances"][0]["frame_kvps"] == [110.0, 110.0, 110.0]
    assert data["instances"][0]["kvp_source"] == "top_level"
    assert data["voltage_distribution"] == [
        {"kvp": 110.0, "frame_count": 3}
    ]


def test_kvp_mixed_series_across_instances(client):
    # 同一系列内单帧 120kV + 多帧增强 80/120kV：分布按帧汇总
    files = {
        "single.dcm": _image_bytes("1.1.1", kvp=120),
        "mf.dcm": _image_bytes(
            "1.1.2", number_of_frames=2, per_frame_kvps=[80, 120]
        ),
    }
    job = _upload(client, files)
    data = _kvp(client, job["id"]).json()
    assert data["instance_count"] == 2
    assert data["total_frames"] == 3
    assert data["mixed_kvp"] is True
    assert data["voltage_distribution"] == [
        {"kvp": 80.0, "frame_count": 1},
        {"kvp": 120.0, "frame_count": 2},
    ]


# --------------------------------------------------------------------------- #
# 404 / 409
# --------------------------------------------------------------------------- #
def test_kvp_job_not_found_404(client):
    r = client.get(f"/jobs/deadbeef/series/{_anon(SERIES)}/kvp")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "JOB_NOT_FOUND"
    assert detail["series_instance_uid"] == _anon(SERIES)


def test_kvp_series_not_found_404(client):
    job = _upload(client, {"a.dcm": _image_bytes("1.1.1", kvp=120)})
    r = _kvp(client, job["id"], series_uid="2.999")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "SERIES_NOT_FOUND"
    assert detail["series_instance_uid"] == _anon("2.999")


def test_kvp_failed_job_409(client):
    r = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": SECRET},
    )
    job = r.json()
    assert job["status"] == "failed"
    resp = client.get(f"/jobs/{job['id']}/series/x/kvp")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "JOB_NOT_READY"
    assert detail["series_instance_uid"] == "x"


def _conflict(client, files):
    job = _upload(client, files)
    r = _kvp(client, job["id"])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "SERIES_KVP_CONFLICT"
    assert detail["series_instance_uid"] == _anon(SERIES)
    return detail


def test_conflict_frame_count_mismatch(client):
    # 声明 3 帧，逐帧序列只有 2 个条目
    files = {
        "mf.dcm": _image_bytes(
            "1.1.1", number_of_frames=3, per_frame_kvps=[80, 120]
        ),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "frame_count_mismatch"]
    assert len(problems) == 1
    p = problems[0]
    assert p["sop_instance_uid"] == _anon("1.1.1")
    assert p["declared_number_of_frames"] == "3"
    assert p["per_frame_item_count"] == 2
    assert detail["conflict_count"] == len(detail["problems"])


def test_conflict_per_frame_sequence_without_number_of_frames(client):
    # NumberOfFrames 缺省按 1 帧；逐帧序列 2 个条目 -> 不符
    files = {
        "mf.dcm": _image_bytes("1.1.1", per_frame_kvps=[80, 120]),
    }
    detail = _conflict(client, files)
    reasons = [p["reason"] for p in detail["problems"]]
    assert "frame_count_mismatch" in reasons


def test_conflict_invalid_number_of_frames(client):
    # NumberOfFrames 必须为正整数，0 为非法声明
    files = {
        "bad.dcm": _image_bytes("1.1.1", kvp=120, number_of_frames=0),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "invalid_number_of_frames"]
    assert len(problems) == 1
    assert problems[0]["sop_instance_uid"] == _anon("1.1.1")
    assert problems[0]["declared_number_of_frames"] == "0"


def test_conflict_ambiguous_sop_output(client):
    # 输入两个同 SOP UID 的文件 -> 输出 <uid>.dcm / <uid>-2.dcm
    files = {
        "dup_a.dcm": _image_bytes("1.1.77", kvp=120),
        "dup_b.dcm": _image_bytes("1.1.77", kvp=80),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "ambiguous_output"]
    assert len(problems) == 1
    p = problems[0]
    assert p["sop_instance_uid"] == _anon("1.1.77")
    assert p["output_files"] == sorted(
        [f"{_anon('1.1.77')}.dcm", f"{_anon('1.1.77')}-2.dcm"]
    )


def test_conflict_collects_multiple_problems_at_once(client):
    # 一个实例帧数不符、一个实例 NumberOfFrames 非法：同一次 409 全部给出
    files = {
        "ok.dcm": _image_bytes("1.1.1", kvp=120),
        "mf.dcm": _image_bytes(
            "1.1.2", number_of_frames=3, per_frame_kvps=[80, 120]
        ),
        "bad.dcm": _image_bytes("1.1.3", kvp=120, number_of_frames=0),
    }
    detail = _conflict(client, files)
    reasons = {p["reason"] for p in detail["problems"]}
    assert "frame_count_mismatch" in reasons
    assert "invalid_number_of_frames" in reasons
    assert detail["conflict_count"] == len(detail["problems"])


def test_kvp_output_file_removed_conflict(client):
    job = _upload(client, {"a.dcm": _image_bytes("1.1.1", kvp=120)})
    from app.main import registry
    job_obj = registry.get(job["id"])
    target = os.path.join(job_obj.result.output_dir, f"{_anon('1.1.1')}.dcm")
    os.remove(target)
    r = _kvp(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SERIES_KVP_CONFLICT"
    problems = [p for p in detail["problems"] if p["reason"] == "output_missing"]
    assert len(problems) == 1
    assert problems[0]["sop_instance_uid"] == _anon("1.1.1")


def test_kvp_unreadable_output_conflict(client):
    job = _upload(client, {"a.dcm": _image_bytes("1.1.1", kvp=120)})
    from app.main import registry
    job_obj = registry.get(job["id"])
    target = os.path.join(job_obj.result.output_dir, f"{_anon('1.1.1')}.dcm")
    with open(target, "wb") as fh:
        fh.write(b"\x00\x01not dicom anymore")
    r = _kvp(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SERIES_KVP_CONFLICT"
    problems = [p for p in detail["problems"] if p["reason"] == "unreadable"]
    assert len(problems) == 1
    assert problems[0]["sop_instance_uid"] == _anon("1.1.1")


def test_conflict_response_contains_only_anonymized_uids(client):
    """冲突明细不得泄露原始 Series/SOP UID。"""
    files = {
        "mf.dcm": _image_bytes(
            "1.1.1", number_of_frames=3, per_frame_kvps=[80, 120]
        ),
    }
    detail = _conflict(client, files)
    raw = str(detail)
    assert SERIES not in raw
    assert "1.1.1" not in raw
    assert _anon("1.1.1") in raw


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
def test_kvp_endpoint_in_openapi(client):
    r = client.get("/openapi.json")
    paths = r.json()["paths"]
    key = "/jobs/{job_id}/series/{series_instance_uid}/kvp"
    assert key in paths
    assert "get" in paths[key]
    schema = paths[key]["get"]
    assert "SeriesKvpOut" in str(schema["responses"]["200"])
    # 根路径端点清单也包含新接口
    endpoints = client.get("/").json()["endpoints"]
    assert "GET /jobs/{job_id}/series/{series_instance_uid}/kvp" in endpoints
