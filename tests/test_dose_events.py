"""GET /jobs/{id}/instances/{sop_uid}/dose-events：匿名 CT 剂量 SR 照射事件解析。"""
from __future__ import annotations

import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    XRayRadiationDoseSRStorage,
)

from app.crypto import derive_uid
from tests.sample_data import _write_bytes

SECRET = "dose-secret-123456"

STUDY = "3.1"
SERIES = "2.1"
SR_SOP = "1.1.1"

_ABSENT = object()  # 哨兵：不写入该元素


# --------------------------------------------------------------------------- #
# 合成 DICOM：X-Ray Radiation Dose SR
# --------------------------------------------------------------------------- #
def _code(value, meaning, designator="DCM") -> Dataset:
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = designator
    item.CodeMeaning = meaning
    return item


def _container(code_value, meaning, children) -> Dataset:
    item = Dataset()
    item.ValueType = "CONTAINER"
    item.ConceptNameCodeSequence = [_code(code_value, meaning)]
    item.ContentSequence = list(children)
    return item


def _num(code_value, meaning, value="1.0", units="mGy", *,
         with_measured_value=True) -> Dataset:
    """NUM 内容项；value=_ABSENT 时不写 NumericValue，units=None 时不写单位序列。"""
    item = Dataset()
    item.ValueType = "NUM"
    item.ConceptNameCodeSequence = [_code(code_value, meaning)]
    if with_measured_value:
        mv = Dataset()
        if value is not _ABSENT:
            mv.NumericValue = value
        if units is not None:
            mv.MeasurementUnitsCodeSequence = [_code(units, units, "UCUM")]
        item.MeasuredValueSequence = [mv]
    return item


def _event(*children) -> Dataset:
    return _container("113812", "CT Irradiation Event Data", children)


def _ctdi_vol(value="12.5", units="mGy", **kw) -> Dataset:
    return _num("113829", "CTDIvol", value, units, **kw)


def _dlp(value="456.5", units="mGy.cm", **kw) -> Dataset:
    return _num("113838", "DLP", value, units, **kw)


def _accumulated(*children) -> Dataset:
    return _container("113811", "CT Accumulated Dose Data", children)


def _dlp_total(value="9999.9") -> Dataset:
    return _num("113813", "CT Dose Length Product Total", value, "mGy.cm")


def _dose_sr_bytes(sop_uid: str = SR_SOP, content=(), *,
                   with_content_sequence: bool = True) -> bytes:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = XRayRadiationDoseSRStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = XRayRadiationDoseSRStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = STUDY
    ds.SeriesInstanceUID = SERIES
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.Modality = "SR"
    if with_content_sequence:
        ds.ContentSequence = list(content)
    return _write_bytes(ds)


def _ct_image_bytes(sop_uid: str) -> bytes:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = STUDY
    ds.SeriesInstanceUID = SERIES
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.Modality = "CT"
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
        files={"file": ("dose.zip", _zip_bytes(files), "application/zip")},
        data={"secret": SECRET, "shift_dates": "false"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _anon(original: str) -> str:
    return derive_uid(SECRET, original)


def _dose_events(client, job_id: str, sop_uid: str = SR_SOP):
    return client.get(f"/jobs/{job_id}/instances/{_anon(sop_uid)}/dose-events")


def _output_path(client, job_id: str, sop_uid: str = SR_SOP) -> str:
    from app.main import registry
    job_obj = registry.get(job_id)
    return os.path.join(job_obj.result.output_dir, f"{_anon(sop_uid)}.dcm")


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
def test_dose_events_basic_two_events(client):
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _accumulated(_dlp_total("9999.9")),  # 报告级累计值：必须忽略
            _event(_ctdi_vol("12.5"), _dlp("456.5")),
            _event(_ctdi_vol("8.25"), _dlp("250.5")),
        ]),
    }
    job = _upload(client, files)
    r = _dose_events(client, job["id"])
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["sop_instance_uid"] == _anon(SR_SOP)
    assert data["output_file"] == f"{_anon(SR_SOP)}.dcm"
    assert data["event_count"] == 2
    assert data["events"] == [
        {"index": 0, "ctdi_vol_mgy": 12.5, "dlp_mgy_cm": 456.5},
        {"index": 1, "ctdi_vol_mgy": 8.25, "dlp_mgy_cm": 250.5},
    ]
    # 合计只含事件 DLP，不含报告级累计值 9999.9
    assert data["total_dlp_mgy_cm"] == pytest.approx(707.0)


def test_dose_events_stray_top_level_values_ignored(client):
    # 事件边界之外的 CTDIvol/DLP（报告级）一律忽略
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _ctdi_vol("99.0"),
            _dlp("8888.0"),
            _event(_ctdi_vol("10.0"), _dlp("100.0")),
        ]),
    }
    job = _upload(client, files)
    data = _dose_events(client, job["id"]).json()
    assert data["event_count"] == 1
    assert data["events"][0]["ctdi_vol_mgy"] == 10.0
    assert data["events"][0]["dlp_mgy_cm"] == 100.0
    assert data["total_dlp_mgy_cm"] == pytest.approx(100.0)


def test_dose_events_nested_values_attributed_to_event(client):
    # 事件容器内任意嵌套深度的数值都归属该事件
    nested = _container("113820", "CT Acquisition", [
        _container("113822", "CT Acquisition Parameters", [
            _ctdi_vol("6.5"),
            _dlp("321.5"),
        ]),
    ])
    files = {"sr.dcm": _dose_sr_bytes(content=[_event(nested)])}
    job = _upload(client, files)
    data = _dose_events(client, job["id"]).json()
    assert data["events"] == [
        {"index": 0, "ctdi_vol_mgy": 6.5, "dlp_mgy_cm": 321.5}
    ]
    assert data["total_dlp_mgy_cm"] == pytest.approx(321.5)


def test_dose_events_content_order_preserved(client):
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol("1.0"), _dlp("100.0")),
            _event(_ctdi_vol("2.0"), _dlp("50.0")),
            _event(_ctdi_vol("3.0"), _dlp("300.0")),
        ]),
    }
    job = _upload(client, files)
    data = _dose_events(client, job["id"]).json()
    assert [e["dlp_mgy_cm"] for e in data["events"]] == [100.0, 50.0, 300.0]
    assert [e["index"] for e in data["events"]] == [0, 1, 2]
    assert data["total_dlp_mgy_cm"] == pytest.approx(450.0)


def test_dose_events_zero_events_ok(client):
    # 剂量 SR 中没有任何 CT 照射事件容器：合法结果为空列表、合计 0
    files = {"sr.dcm": _dose_sr_bytes(content=[_accumulated(_dlp_total("42.0"))])}
    job = _upload(client, files)
    r = _dose_events(client, job["id"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["event_count"] == 0
    assert data["events"] == []
    assert data["total_dlp_mgy_cm"] == 0.0


def test_dose_events_nested_event_boundary(client):
    # 非标准嵌套：内层事件容器成为新边界，其后的兄弟节点仍归属外层事件
    inner = _event(_ctdi_vol("2.0"), _dlp("20.0"))
    outer = _event(_ctdi_vol("1.0"), inner, _dlp("10.0"))
    files = {"sr.dcm": _dose_sr_bytes(content=[outer])}
    job = _upload(client, files)
    data = _dose_events(client, job["id"]).json()
    assert data["events"] == [
        {"index": 0, "ctdi_vol_mgy": 1.0, "dlp_mgy_cm": 10.0},
        {"index": 1, "ctdi_vol_mgy": 2.0, "dlp_mgy_cm": 20.0},
    ]
    assert data["total_dlp_mgy_cm"] == pytest.approx(30.0)


def test_dose_events_only_requested_instance_parsed(client):
    files = {
        "sr1.dcm": _dose_sr_bytes(SR_SOP, content=[
            _event(_ctdi_vol("1.0"), _dlp("10.0")),
        ]),
        "sr2.dcm": _dose_sr_bytes("1.1.2", content=[
            _event(_ctdi_vol("9.0"), _dlp("90.0")),
            _event(_ctdi_vol("9.0"), _dlp("90.0")),
        ]),
    }
    job = _upload(client, files)
    data = _dose_events(client, job["id"], SR_SOP).json()
    assert data["sop_instance_uid"] == _anon(SR_SOP)
    assert data["event_count"] == 1
    assert data["total_dlp_mgy_cm"] == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# 404
# --------------------------------------------------------------------------- #
def test_dose_events_job_not_found_404(client):
    r = client.get(f"/jobs/deadbeef/instances/{_anon(SR_SOP)}/dose-events")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "JOB_NOT_FOUND"
    assert detail["sop_instance_uid"] == _anon(SR_SOP)


def test_dose_events_instance_not_found_404(client):
    job = _upload(client, {"sr.dcm": _dose_sr_bytes(content=[])})
    r = client.get(f"/jobs/{job['id']}/instances/{_anon('9.9.9')}/dose-events")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "INSTANCE_NOT_FOUND"
    assert detail["sop_instance_uid"] == _anon("9.9.9")


def test_dose_events_output_file_removed_404(client):
    job = _upload(client, {
        "sr.dcm": _dose_sr_bytes(content=[_event(_ctdi_vol(), _dlp())]),
    })
    os.remove(_output_path(client, job["id"]))
    r = _dose_events(client, job["id"])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "INSTANCE_OUTPUT_MISSING"
    assert detail["reason"] == "output_missing"
    assert detail["sop_instance_uid"] == _anon(SR_SOP)


# --------------------------------------------------------------------------- #
# 409：任务/输出定位
# --------------------------------------------------------------------------- #
def test_dose_events_failed_job_409(client):
    r = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": SECRET},
    )
    job = r.json()
    assert job["status"] == "failed"
    resp = client.get(f"/jobs/{job['id']}/instances/x/dose-events")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "JOB_NOT_READY"
    assert detail["sop_instance_uid"] == "x"


def test_dose_events_ambiguous_output_409(client):
    # 输入两个同 SOP UID 的文件 -> 输出 <uid>.dcm / <uid>-2.dcm
    files = {
        "dup_a.dcm": _dose_sr_bytes(SR_SOP, content=[
            _event(_ctdi_vol(), _dlp()),
        ]),
        "dup_b.dcm": _dose_sr_bytes(SR_SOP, content=[
            _event(_ctdi_vol(), _dlp()),
        ]),
    }
    job = _upload(client, files)
    r = _dose_events(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "AMBIGUOUS_INSTANCE_OUTPUT"
    assert detail["reason"] == "ambiguous_output"
    assert detail["sop_instance_uid"] == _anon(SR_SOP)
    assert detail["output_files"] == sorted(
        [f"{_anon(SR_SOP)}.dcm", f"{_anon(SR_SOP)}-2.dcm"]
    )


def test_dose_events_not_dose_sr_409(client):
    job = _upload(client, {"ct.dcm": _ct_image_bytes(SR_SOP)})
    r = _dose_events(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "DOSE_SR_CONFLICT"
    problems = [p for p in detail["problems"] if p["reason"] == "not_dose_sr"]
    assert len(problems) == 1
    assert problems[0]["sop_instance_uid"] == _anon(SR_SOP)


def test_dose_events_missing_content_sequence_409(client):
    job = _upload(client, {
        "sr.dcm": _dose_sr_bytes(with_content_sequence=False),
    })
    r = _dose_events(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "DOSE_SR_CONFLICT"
    assert any(p["reason"] == "not_dose_sr" for p in detail["problems"])


def test_dose_events_unreadable_output_409(client):
    job = _upload(client, {
        "sr.dcm": _dose_sr_bytes(content=[_event(_ctdi_vol(), _dlp())]),
    })
    with open(_output_path(client, job["id"]), "wb") as fh:
        fh.write(b"\x00\x01not dicom anymore")
    r = _dose_events(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "DOSE_SR_CONFLICT"
    problems = [p for p in detail["problems"] if p["reason"] == "unreadable"]
    assert len(problems) == 1
    assert problems[0]["sop_instance_uid"] == _anon(SR_SOP)


def test_dose_events_sop_uid_mismatch_409(client):
    job = _upload(client, {
        "sr.dcm": _dose_sr_bytes(content=[_event(_ctdi_vol(), _dlp())]),
    })
    # 篡改磁盘上的匿名输出：文件内 SOPInstanceUID 与请求不符
    import pydicom
    path = _output_path(client, job["id"])
    ds = pydicom.dcmread(path)
    ds.SOPInstanceUID = "2.25.999999999"
    ds.save_as(path, enforce_file_format=True)
    r = _dose_events(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "DOSE_SR_CONFLICT"
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "sop_instance_uid_mismatch"
    ]
    assert len(problems) == 1


# --------------------------------------------------------------------------- #
# 409：事件级问题（缺项 / 重复 / 非有限数值 / 单位不符）
# --------------------------------------------------------------------------- #
def _conflict(client, files):
    job = _upload(client, files)
    r = _dose_events(client, job["id"])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "DOSE_SR_CONFLICT"
    assert detail["sop_instance_uid"] == _anon(SR_SOP)
    # 不输出部分事件或部分合计
    assert "events" not in detail
    assert "total_dlp_mgy_cm" not in detail
    assert detail["conflict_count"] == len(detail["problems"])
    return detail


def test_conflict_missing_dlp(client):
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol("12.5"), _dlp("456.5")),
            _event(_ctdi_vol("8.0")),  # 缺 DLP
        ]),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"] if p["reason"] == "missing_value"]
    assert len(problems) == 1
    p = problems[0]
    assert p["event_index"] == 1
    assert p["quantity"] == "dlp"
    assert p["sop_instance_uid"] == _anon(SR_SOP)


def test_conflict_missing_ctdi_vol(client):
    files = {"sr.dcm": _dose_sr_bytes(content=[_event(_dlp("100.0"))])}
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"] if p["reason"] == "missing_value"]
    assert len(problems) == 1
    assert problems[0]["event_index"] == 0
    assert problems[0]["quantity"] == "ctdi_vol"


def test_conflict_missing_both_quantities_listed_together(client):
    files = {"sr.dcm": _dose_sr_bytes(content=[_event()])}
    detail = _conflict(client, files)
    quantities = {
        p["quantity"] for p in detail["problems"]
        if p["reason"] == "missing_value"
    }
    assert quantities == {"ctdi_vol", "dlp"}


def test_conflict_duplicate_value(client):
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol("1.0"), _dlp("10.0"), _dlp("20.0")),
        ]),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"] if p["reason"] == "duplicate_value"]
    assert len(problems) == 1
    assert problems[0]["event_index"] == 0
    assert problems[0]["quantity"] == "dlp"


def test_conflict_invalid_value_non_finite(client):
    # DS 文本合法但解析为非有限数值（1e999 -> inf）
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol("1e999"), _dlp("10.0")),
        ]),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"] if p["reason"] == "invalid_value"]
    assert len(problems) == 1
    p = problems[0]
    assert p["event_index"] == 0
    assert p["quantity"] == "ctdi_vol"
    assert p["numeric_value"] == "1e999"


def test_conflict_invalid_value_unparseable_multivalue(client):
    # VM>1 的畸形 NumericValue：无法解析为单一有限数值
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol("1.0"), _dlp(["1.5", "2.5"])),
        ]),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"] if p["reason"] == "invalid_value"]
    assert len(problems) == 1
    assert problems[0]["quantity"] == "dlp"


def test_conflict_invalid_value_missing_numeric_value(client):
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol(_ABSENT), _dlp("10.0")),
        ]),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"] if p["reason"] == "invalid_value"]
    assert len(problems) == 1
    assert problems[0]["quantity"] == "ctdi_vol"


def test_conflict_invalid_value_missing_measured_value_sequence(client):
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol("1.0"), _dlp("10.0", with_measured_value=False)),
        ]),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"] if p["reason"] == "invalid_value"]
    assert len(problems) == 1
    assert problems[0]["quantity"] == "dlp"


def test_conflict_unit_mismatch(client):
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol("1.0", units="mGy.cm"), _dlp("10.0", units="mGy")),
        ]),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"] if p["reason"] == "unit_mismatch"]
    assert len(problems) == 2
    by_quantity = {p["quantity"]: p for p in problems}
    assert by_quantity["ctdi_vol"]["units"] == "mGy.cm"
    assert by_quantity["dlp"]["units"] == "mGy"


def test_conflict_unit_missing(client):
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol("1.0", units=None), _dlp("10.0")),
        ]),
    }
    detail = _conflict(client, files)
    problems = [p for p in detail["problems"] if p["reason"] == "unit_mismatch"]
    assert len(problems) == 1
    assert problems[0]["quantity"] == "ctdi_vol"
    assert problems[0]["units"] is None


def test_conflict_collects_multiple_problems_at_once(client):
    # 一个事件缺 DLP、另一个事件单位不符：同一次 409 全部给出
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol("1.0")),  # 缺 DLP
            _event(_ctdi_vol("2.0"), _dlp("20.0", units="mGy")),  # 单位不符
        ]),
    }
    detail = _conflict(client, files)
    reasons = {(p["reason"], p["event_index"]) for p in detail["problems"]}
    assert ("missing_value", 0) in reasons
    assert ("unit_mismatch", 1) in reasons


def test_conflict_no_partial_totals(client):
    # 一个事件完整、一个事件缺项：整体 409，不输出部分事件/合计
    files = {
        "sr.dcm": _dose_sr_bytes(content=[
            _event(_ctdi_vol("1.0"), _dlp("10.0")),
            _event(_dlp("20.0")),  # 缺 CTDIvol
        ]),
    }
    detail = _conflict(client, files)
    assert detail["problems"]
    assert all(p["sop_instance_uid"] == _anon(SR_SOP) for p in detail["problems"])


def test_conflict_response_contains_only_anonymized_uids(client):
    """冲突明细不得泄露原始 SOP UID。"""
    files = {"sr.dcm": _dose_sr_bytes(content=[_event(_ctdi_vol("1.0"))])}
    detail = _conflict(client, files)
    raw = str(detail)
    assert SR_SOP not in raw
    assert _anon(SR_SOP) in raw


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
def test_dose_events_endpoint_in_openapi(client):
    r = client.get("/openapi.json")
    paths = r.json()["paths"]
    key = "/jobs/{job_id}/instances/{sop_instance_uid}/dose-events"
    assert key in paths
    assert "get" in paths[key]
    schema = paths[key]["get"]
    assert "DoseEventsOut" in str(schema["responses"]["200"])
    # 根路径端点清单也包含新接口
    endpoints = client.get("/").json()["endpoints"]
    assert "GET /jobs/{job_id}/instances/{sop_instance_uid}/dose-events" in endpoints
