"""GET /jobs/{id}/series/{series_uid}/geometry：法线投影排序、缺口判定与冲突明细。"""
from __future__ import annotations

import io
import os
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    RTStructureSetStorage,
)

from app.crypto import derive_uid
from tests.sample_data import _write_bytes

SECRET = "geometry-secret-123456"

STUDY = "3.1"
SERIES = "2.1"
FRAME = "4.1"
# 标准轴位：行方向 X、列方向 Y，叉积法线 +Z
AXIAL_IOP = (1, 0, 0, 0, 1, 0)


# --------------------------------------------------------------------------- #
# 合成 DICOM
# --------------------------------------------------------------------------- #
def _image_bytes(
    sop_uid: str,
    *,
    series_uid: str = SERIES,
    study_uid: str = STUDY,
    frame_uid: str | None = FRAME,
    ipp=(0, 0, 0),
    iop=AXIAL_IOP,
    number_of_frames: int | None = None,
    drop: tuple[str, ...] = (),
    sop_class=CTImageStorage,
    modality: str = "CT",
    pixels: bytes | None = None,
):
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = sop_class
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    if frame_uid is not None:
        ds.FrameOfReferenceUID = frame_uid
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.Modality = modality
    ds.ImageOrientationPatient = list(iop)
    ds.ImagePositionPatient = list(ipp)
    if number_of_frames is not None:
        ds.NumberOfFrames = number_of_frames
    if pixels is not None:
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.Rows = ds.Columns = 2
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.PixelData = pixels
    for keyword in drop:
        if keyword in ds:
            del ds[keyword]
    return _write_bytes(ds)


def _axial_series(z_positions, *, series_uid=SERIES, frame_uid=FRAME):
    return {
        f"slice_{i:02d}.dcm": _image_bytes(
            f"1.1.{i}", series_uid=series_uid, frame_uid=frame_uid,
            ipp=(10.0, -20.0, float(z)),
        )
        for i, z in enumerate(z_positions)
    }


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


def _geometry(client, job_id: str, series_uid: str = SERIES):
    anon_series = derive_uid(SECRET, series_uid)
    return client.get(f"/jobs/{job_id}/series/{anon_series}/geometry")


def _anon(original: str) -> str:
    return derive_uid(SECRET, original)


# --------------------------------------------------------------------------- #
# 成功路径：投影排序与缺口
# --------------------------------------------------------------------------- #
def test_geometry_even_spacing_no_gaps(client):
    # 上传顺序故意打乱，验证按投影而非文件名排序
    files = {
        "z30.dcm": _image_bytes("1.1.3", ipp=(0, 0, 30)),
        "z00.dcm": _image_bytes("1.1.0", ipp=(0, 0, 0)),
        "z10.dcm": _image_bytes("1.1.1", ipp=(0, 0, 10)),
        "z20.dcm": _image_bytes("1.1.2", ipp=(0, 0, 20)),
    }
    job = _upload(client, files)
    r = _geometry(client, job["id"])
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["series_instance_uid"] == _anon(SERIES)
    assert data["frame_of_reference_uid"] == _anon(FRAME)
    assert data["image_orientation_patient"] == [1, 0, 0, 0, 1, 0]
    assert data["normal"] == [0, 0, 1]
    assert data["instance_count"] == 4
    assert data["gap_check"] == "ok"
    assert data["median_spacing"] == pytest.approx(10.0)
    assert data["gap_factor"] == 1.5
    assert data["anomalous_gap_indices"] == []
    assert data["message"] is None
    assert data["excluded_instances"] == []

    assert [s["sop_instance_uid"] for s in data["slices"]] == [
        _anon(f"1.1.{i}") for i in range(4)
    ]
    assert [s["projection"] for s in data["slices"]] == [0, 10, 20, 30]
    assert [s["index"] for s in data["slices"]] == [0, 1, 2, 3]
    assert len(data["spacings"]) == 3
    assert [sp["distance"] for sp in data["spacings"]] == [10, 10, 10]
    assert all(not sp["anomalous"] for sp in data["spacings"])
    for i, sp in enumerate(data["spacings"]):
        assert sp["index"] == i
        assert sp["from_sop_instance_uid"] == _anon(f"1.1.{i}")
        assert sp["to_sop_instance_uid"] == _anon(f"1.1.{i + 1}")


def test_geometry_gap_larger_than_1p5_median_flagged(client):
    # 间距 5, 5, 20：中位数 5，20 > 7.5 -> 异常间隙索引 2
    job = _upload(client, _axial_series([0, 5, 10, 30]))
    data = _geometry(client, job["id"]).json()
    assert data["median_spacing"] == pytest.approx(5.0)
    assert data["anomalous_gap_indices"] == [2]
    flags = [sp["anomalous"] for sp in data["spacings"]]
    assert flags == [False, False, True]
    assert data["spacings"][2]["distance"] == pytest.approx(20.0)


def test_geometry_gap_exactly_at_threshold_not_flagged(client):
    # 间距 4, 6, 6：中位数 6，4 与 6 均不大于 1.5*6=9
    job = _upload(client, _axial_series([0, 4, 10, 16]))
    data = _geometry(client, job["id"]).json()
    assert data["median_spacing"] == pytest.approx(6.0)
    assert data["anomalous_gap_indices"] == []


def test_geometry_median_with_even_number_of_spacings(client):
    # 5 张 -> 4 个间距 [5, 5, 20, 20]，偶数个间距取中间两值平均 = 12.5；
    # 5 <= 18.75，20 > 18.75 -> 后两个间隙异常
    job = _upload(client, _axial_series([0, 5, 10, 30, 50]))
    data = _geometry(client, job["id"]).json()
    assert data["median_spacing"] == pytest.approx(12.5)
    assert data["anomalous_gap_indices"] == [2, 3]


def test_geometry_sagittal_orientation_normal(client):
    # 矢状位：行方向 Y、列方向 Z -> 法线 -X（叉积 (0,1,0)×(0,0,1) = (1,0,0)）
    iop = (0, 1, 0, 0, 0, 1)
    files = {
        f"s{i}.dcm": _image_bytes(
            f"1.1.{i}", ipp=(float(x), 0, 0), iop=iop
        )
        for i, x in enumerate([0, 3, 6])
    }
    job = _upload(client, files)
    data = _geometry(client, job["id"]).json()
    assert data["normal"] == [pytest.approx(1), 0, 0]
    assert [s["projection"] for s in data["slices"]] == [0, 3, 6]
    assert data["gap_check"] == "ok"


def test_geometry_non_axis_normal(client):
    # 法线带斜分量：行 (1,1,0)/√2、列 Z -> 法线 (1,-1,0)/√2
    iop = (2 ** -0.5, 2 ** -0.5, 0, 0, 0, 1)
    files = {
        f"s{i}.dcm": _image_bytes(
            f"1.1.{i}", ipp=(float(i), 0, 0), iop=iop
        )
        for i in range(3)
    }
    job = _upload(client, files)
    data = _geometry(client, job["id"]).json()
    sqrt2 = 2 ** -0.5
    assert data["normal"][0] == pytest.approx(sqrt2)
    assert data["normal"][1] == pytest.approx(-sqrt2)
    assert data["normal"][2] == pytest.approx(0)
    # 位置 (i,0,0) 在法线上的投影 = i/√2
    assert [s["projection"] for s in data["slices"]] == pytest.approx(
        [0, sqrt2, 2 * sqrt2]
    )


def test_geometry_oblique_positions_sorted_along_normal(client):
    # 行/列张成倾斜 45° 的竖直面，法线 (1,-1,0)/√2；
    # 片层位置沿法线方向移动，投影应单调为 0/4/8
    iop = (2 ** -0.5, 2 ** -0.5, 0, 0, 0, 1)
    normal = np.cross(np.array(iop[:3]), np.array(iop[3:]))
    files = {}
    for i in range(3):
        pos = normal * (i * 4.0)
        files[f"s{i}.dcm"] = _image_bytes(
            f"1.1.{i}", ipp=tuple(float(v) for v in pos), iop=iop
        )
    job = _upload(client, files)
    data = _geometry(client, job["id"]).json()
    assert [s["projection"] for s in data["slices"]] == pytest.approx([0, 4, 8])
    assert data["median_spacing"] == pytest.approx(4.0)


def test_geometry_only_requested_series_analyzed(client):
    # 同 ZIP 内含另一个系列；几何分析只看目标系列
    files = _axial_series([0, 5, 10])
    files["other.dcm"] = _image_bytes(
        "1.9.9", series_uid="2.9", frame_uid="4.9",
        iop=(0, 1, 0, 0, 0, 1), ipp=(99, 0, 0),
    )
    job = _upload(client, files)
    data = _geometry(client, job["id"]).json()
    assert data["instance_count"] == 3
    assert {s["sop_instance_uid"] for s in data["slices"]} == {
        _anon(f"1.1.{i}") for i in range(3)
    }


def test_geometry_multiframe_excluded_but_analysis_succeeds(client):
    files = _axial_series([0, 5, 10])
    files["mf.dcm"] = _image_bytes("1.1.9", ipp=(0, 0, 99), number_of_frames=8)
    job = _upload(client, files)
    r = _geometry(client, job["id"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["instance_count"] == 3
    excluded = data["excluded_instances"]
    assert len(excluded) == 1
    assert excluded[0]["sop_instance_uid"] == _anon("1.1.9")
    assert excluded[0]["reason"] == "multiframe"
    assert excluded[0]["number_of_frames"] == "8"
    assert excluded[0]["output_file"].endswith(".dcm")


def test_geometry_number_of_frames_one_is_single_frame(client):
    files = _axial_series([0, 5, 10])
    files["one.dcm"] = _image_bytes(
        "1.1.9", ipp=(0, 0, 15), number_of_frames=1
    )
    job = _upload(client, files)
    data = _geometry(client, job["id"]).json()
    assert data["instance_count"] == 4
    assert data["excluded_instances"] == []


def test_geometry_one_and_two_slices_insufficient(client):
    for n, zs in ((1, [0]), (2, [0, 5])):
        job = _upload(client, _axial_series(zs))
        data = _geometry(client, job["id"]).json()
        assert data["gap_check"] == "insufficient_slices"
        assert data["median_spacing"] is None
        assert data["anomalous_gap_indices"] == []
        assert data["instance_count"] == n
        assert "不足 3 张" in data["message"]
        assert len(data["spacings"]) == n - 1


def test_geometry_duplicate_uploaded_slices_in_reverse_order(client):
    # 逆序位置上传仍按投影排序
    job = _upload(client, _axial_series([30, 20, 10, 0]))
    data = _geometry(client, job["id"]).json()
    assert [s["projection"] for s in data["slices"]] == [0, 10, 20, 30]


def test_geometry_uses_anonymized_output_only(client, tmp_path):
    """响应只含匿名 UID；匿名文件里的坐标与原始一致（匿名化不改几何标签）。"""
    from tests.sample_data import build_sample_zip

    zip_path = tmp_path / "sample.zip"
    meta = build_sample_zip(str(zip_path), seed=7)
    with open(zip_path, "rb") as fh:
        r = client.post(
            "/jobs",
            files={"file": ("study.zip", fh, "application/zip")},
            data={"secret": SECRET, "shift_dates": "false"},
        )
    assert r.status_code == 201, r.text
    job = r.json()
    # 样例 ZIP 的两张 CT 均缺 IPP/IOP -> invalid_geometry 409（见冲突用例）
    g = client.get(
        f"/jobs/{job['id']}/series/{derive_uid(SECRET, meta['series_uid'])}/geometry"
    )
    assert g.status_code == 409


# --------------------------------------------------------------------------- #
# 404 / 409
# --------------------------------------------------------------------------- #
def test_geometry_job_not_found_404(client):
    r = client.get(f"/jobs/deadbeef/series/{_anon(SERIES)}/geometry")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "JOB_NOT_FOUND"
    assert detail["series_instance_uid"] == _anon(SERIES)


def test_geometry_series_not_found_404(client):
    job = _upload(client, _axial_series([0, 5, 10]))
    r = _geometry(client, job["id"], series_uid="2.999")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "SERIES_NOT_FOUND"
    assert detail["series_instance_uid"] == _anon("2.999")


def test_geometry_series_with_only_failed_files_404(client):
    # 系列存在但所有文件匿名化失败（坏 DICOM）-> 成功输出为空 -> 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bad.dcm", b"\x00\x01not dicom")
    r = client.post(
        "/jobs",
        files={"file": ("b.zip", buf.getvalue(), "application/zip")},
        data={"secret": SECRET, "shift_dates": "false"},
    )
    job = r.json()
    # 该任务无任何成功输出；任意系列 UID 都应 404
    resp = client.get(f"/jobs/{job['id']}/series/2.25.whatever/geometry")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "SERIES_NOT_FOUND"


def test_geometry_all_multiframe_series_409(client):
    files = {
        "m1.dcm": _image_bytes("1.1.1", ipp=(0, 0, 0), number_of_frames=8),
        "m2.dcm": _image_bytes("1.1.2", ipp=(0, 0, 5), number_of_frames=16),
    }
    job = _upload(client, files)
    r = _geometry(client, job["id"])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "NO_SINGLE_FRAME_INSTANCES"
    assert detail["single_frame_instance_count"] == 0
    assert {p["reason"] for p in detail["problems"]} == {"multiframe"}
    assert {p["number_of_frames"] for p in detail["problems"]} == {"8", "16"}


def test_geometry_failed_job_409(client):
    r = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": SECRET},
    )
    job = r.json()
    assert job["status"] == "failed"
    resp = client.get(f"/jobs/{job['id']}/series/x/geometry")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "JOB_NOT_READY"
    assert detail["series_instance_uid"] == "x"


# --------------------------------------------------------------------------- #
# 409：几何冲突明细
# --------------------------------------------------------------------------- #
def _conflict(client, files, series_uid=SERIES):
    job = _upload(client, files)
    r = _geometry(client, job["id"], series_uid=series_uid)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "SERIES_GEOMETRY_CONFLICT"
    assert detail["series_instance_uid"] == _anon(series_uid)
    return detail


def _reasons(detail):
    return [p["reason"] for p in detail["problems"]]


def test_conflict_frame_of_reference_mismatch(client):
    files = _axial_series([0, 5], frame_uid=FRAME)
    files["c.dcm"] = _image_bytes("1.1.2", frame_uid="4.2", ipp=(0, 0, 10))
    files["d.dcm"] = _image_bytes("1.1.3", frame_uid="4.2", ipp=(0, 0, 15))
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "frame_of_reference_mismatch"]
    assert len(problems) == 1
    p = problems[0]
    member_uids = sorted([_anon("1.1.2"), _anon("1.1.3")])
    assert p["sop_instance_uids"] == member_uids
    assert p["sop_instance_uid"] == member_uids[0]
    assert p["frame_of_reference_uid"] == _anon("4.2")
    assert p["expected_frame_of_reference_uid"] == _anon(FRAME)


def test_conflict_orientation_mismatch(client):
    files = _axial_series([0, 5, 10])
    # 第 4 张为冠状位（法线 -Y）
    files["d.dcm"] = _image_bytes(
        "1.1.3", ipp=(0, 30, 0), iop=(1, 0, 0, 0, 0, -1)
    )
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "orientation_mismatch"]
    assert len(problems) == 1
    p = problems[0]
    assert p["sop_instance_uids"] == [_anon("1.1.3")]
    assert p["opposite_direction"] is False
    assert p["image_orientation_patient"] == [1, 0, 0, 0, 0, -1]
    assert p["expected_image_orientation_patient"] == [1, 0, 0, 0, 1, 0]
    # 叉积 (1,0,0)×(0,0,-1) = (0,1,0)
    assert p["normal"] == [0, 1, 0]
    assert p["expected_normal"] == [0, 0, 1]


def test_conflict_orientation_180_rotation_in_plane_flagged(client):
    # 行、列同时反向（绕法线旋转 180°）：叉积法线不变，opposite_direction=false，
    # 但 ImageOrientationPatient 方向余弦与系列基准不一致，仍必须报冲突
    files = _axial_series([0, 5, 10])
    files["d.dcm"] = _image_bytes(
        "1.1.3", ipp=(0, 0, 30), iop=(-1, 0, 0, 0, -1, 0)
    )
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "orientation_mismatch"]
    assert len(problems) == 1
    assert problems[0]["opposite_direction"] is False
    assert problems[0]["image_orientation_patient"] == [-1, 0, 0, 0, -1, 0]
    assert problems[0]["expected_image_orientation_patient"] == [1, 0, 0, 0, 1, 0]
    # (-x)×(-y) = +z：法线与基准相同
    assert problems[0]["normal"] == [0, 0, 1]
    assert problems[0]["expected_normal"] == [0, 0, 1]


def test_conflict_orientation_flipped_normal(client):
    # 只反向列方向：叉积法线反向（opposite_direction=true），投影方向相反
    files = _axial_series([0, 5, 10])
    files["d.dcm"] = _image_bytes(
        "1.1.3", ipp=(0, 0, 30), iop=(1, 0, 0, 0, -1, 0)
    )
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "orientation_mismatch"]
    assert len(problems) == 1
    assert problems[0]["opposite_direction"] is True
    # (1,0,0)×(0,-1,0) = (0,0,-1)
    assert problems[0]["normal"] == [0, 0, -1]


def test_conflict_duplicate_projection(client):
    files = _axial_series([0, 5])
    files["dup.dcm"] = _image_bytes("1.1.7", ipp=(0, 0, 5))  # 与第 2 张同层
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "duplicate_projection"]
    assert len(problems) == 1
    p = problems[0]
    assert set(p["sop_instance_uids"]) == {_anon("1.1.1"), _anon("1.1.7")}
    assert p["projection"] == pytest.approx(5.0)
    assert {item["sop_instance_uid"] for item in p["positions"]} == {
        _anon("1.1.1"), _anon("1.1.7")
    }
    assert all(item["projection"] == pytest.approx(5.0) for item in p["positions"])


def test_conflict_duplicate_projection_three_coincident(client):
    files = _axial_series([0])
    files["d1.dcm"] = _image_bytes("1.1.7", ipp=(3, 3, 5))
    files["d2.dcm"] = _image_bytes("1.1.8", ipp=(8, -2, 5))
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "duplicate_projection"]
    assert len(problems) == 1
    assert set(problems[0]["sop_instance_uids"]) == {
        _anon("1.1.7"), _anon("1.1.8")
    }


def test_conflict_invalid_geometry_missing_iop(client):
    files = _axial_series([0, 5, 10])
    files["bad.dcm"] = _image_bytes(
        "1.1.9", ipp=(0, 0, 15), drop=("ImageOrientationPatient",)
    )
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "invalid_geometry"]
    assert len(problems) == 1
    p = problems[0]
    assert p["sop_instance_uid"] == _anon("1.1.9")
    assert "ImageOrientationPatient" in p["missing_or_invalid_tags"]
    assert p["output_file"].endswith(".dcm")


def test_conflict_invalid_geometry_missing_ipp(client):
    files = _axial_series([0, 5, 10])
    files["bad.dcm"] = _image_bytes(
        "1.1.9", ipp=(0, 0, 15), drop=("ImagePositionPatient",)
    )
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "invalid_geometry"]
    assert "ImagePositionPatient" in problems[0]["missing_or_invalid_tags"]


def test_conflict_invalid_geometry_missing_frame_uid(client):
    files = _axial_series([0, 5, 10])
    files["bad.dcm"] = _image_bytes(
        "1.1.9", ipp=(0, 0, 15), frame_uid=None
    )
    detail = _conflict(client, files)
    reasons = _reasons(detail)
    assert "invalid_geometry" in reasons
    p = next(p for p in detail["problems"]
             if p["reason"] == "invalid_geometry")
    assert "FrameOfReferenceUID" in p["missing_or_invalid_tags"]


def test_conflict_invalid_geometry_degenerate_orientation(client):
    files = _axial_series([0, 5, 10])
    # 行方向为零向量：叉积退化
    files["bad.dcm"] = _image_bytes(
        "1.1.9", ipp=(0, 0, 15), iop=(0, 0, 0, 0, 1, 0)
    )
    detail = _conflict(client, files)
    assert "invalid_geometry" in _reasons(detail)


def test_conflict_invalid_geometry_wrong_component_count(client):
    files = _axial_series([0, 5, 10])
    files["bad.dcm"] = _image_bytes(
        "1.1.9", ipp=(0, 0, 15), iop=(1, 0, 0, 0, 1)  # 只有 5 个分量
    )
    detail = _conflict(client, files)
    assert "invalid_geometry" in _reasons(detail)


def test_conflict_non_image_object_without_geometry_tags(client):
    # RTSTRUCT 无 IOP/IPP/FrameOfReferenceUID：非影像对象 -> invalid_geometry
    files = _axial_series([0, 5, 10])
    files["rt.dcm"] = _image_bytes(
        "1.1.9",
        ipp=(0, 0, 0), iop=AXIAL_IOP, frame_uid=None,
        sop_class=RTStructureSetStorage, modality="RTSTRUCT",
        drop=("ImageOrientationPatient", "ImagePositionPatient"),
    )
    detail = _conflict(client, files)
    reasons = _reasons(detail)
    assert "invalid_geometry" in reasons


def test_conflict_problems_contain_only_anonymized_uids(client):
    """冲突明细不得泄露原始 FrameOfReference/Series UID。"""
    files = _axial_series([0, 5], frame_uid=FRAME)
    files["c.dcm"] = _image_bytes("1.1.2", frame_uid="4.2", ipp=(0, 0, 10))
    detail = _conflict(client, files)
    raw = str(detail)
    assert FRAME not in raw            # 原始 FrameOfReferenceUID
    assert "4.2" not in raw            # 原始冲突 UID
    assert SERIES not in raw           # 原始 SeriesInstanceUID
    assert _anon(FRAME) in raw
    assert _anon("4.2") in raw


def test_conflict_collects_multiple_problems_at_once(client):
    # 一张 Frame UID 不同，一张 IOP 缺失：两类问题应在同一次 409 中给出
    files = _axial_series([0, 5, 10], frame_uid=FRAME)
    files["frame.dcm"] = _image_bytes("1.1.8", frame_uid="4.2", ipp=(0, 0, 20))
    files["bad.dcm"] = _image_bytes(
        "1.1.9", ipp=(0, 0, 25), drop=("ImageOrientationPatient",)
    )
    detail = _conflict(client, files)
    reasons = set(_reasons(detail))
    assert "frame_of_reference_mismatch" in reasons
    assert "invalid_geometry" in reasons
    assert detail["conflict_count"] == len(detail["problems"])


def test_conflict_ambiguous_sop_output(client):
    # 输入两个同 SOP UID 的文件 -> 输出 <uid>.dcm / <uid>-2.dcm
    files = _axial_series([0, 5])
    files["dup_a.dcm"] = _image_bytes("1.1.77", ipp=(0, 0, 10))
    files["dup_b.dcm"] = _image_bytes("1.1.77", ipp=(0, 0, 15))
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"]
                if p["reason"] == "ambiguous_output"]
    assert len(problems) == 1
    p = problems[0]
    assert p["sop_instance_uid"] == _anon("1.1.77")
    assert p["output_files"] == sorted(
        [f"{_anon('1.1.77')}.dcm", f"{_anon('1.1.77')}-2.dcm"]
    )


def test_conflict_orientation_mismatch_takes_series_scope(client):
    # 另一个系列方向不同不应影响目标系列
    files = _axial_series([0, 5, 10])
    files["other_series.dcm"] = _image_bytes(
        "9.9.1", series_uid="2.9", frame_uid="4.9",
        ipp=(0, 0, 0), iop=(0, 1, 0, 0, 0, 1),
    )
    job = _upload(client, files)
    r = _geometry(client, job["id"])
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# 输出不可读 / 已删除
# --------------------------------------------------------------------------- #
def test_geometry_output_file_removed_conflict(client):
    job = _upload(client, _axial_series([0, 5, 10]))
    from app.main import registry
    job_obj = registry.get(job["id"])
    # 删除其中一张匿名输出
    target = os.path.join(job_obj.result.output_dir, f"{_anon('1.1.1')}.dcm")
    os.remove(target)
    r = _geometry(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SERIES_GEOMETRY_CONFLICT"
    problems = [p for p in detail["problems"] if p["reason"] == "output_missing"]
    assert len(problems) == 1
    assert problems[0]["sop_instance_uid"] == _anon("1.1.1")


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
def test_geometry_endpoint_in_openapi(client):
    r = client.get("/openapi.json")
    paths = r.json()["paths"]
    key = "/jobs/{job_id}/series/{series_instance_uid}/geometry"
    assert key in paths
    assert "get" in paths[key]
    schema = paths[key]["get"]
    assert "SeriesGeometryOut" in str(schema["responses"]["200"])
    # 根路径端点清单也包含新接口
    endpoints = client.get("/").json()["endpoints"]
    assert (
        "GET /jobs/{job_id}/series/{series_instance_uid}/geometry" in endpoints
    )
