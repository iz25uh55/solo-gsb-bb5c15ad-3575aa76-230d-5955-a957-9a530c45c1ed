"""GET /jobs/{id}/dicomdir/download：DICOMDIR 层级重建、稳定 File ID 与 409 问题明细。"""
from __future__ import annotations

import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.crypto import derive_uid
from tests.sample_data import _write_bytes
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

SECRET = "dicomdir-secret-123456"


def _ct_bytes(
    sop_uid: str,
    series_uid: str,
    study_uid: str,
    patient_id: str = "P-1",
    patient_name: str = "Zhang^San",
    *,
    modality: str = "CT",
    include_optional: bool = True,
    drop: tuple[str, ...] = (),
) -> bytes:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.Modality = modality
    if include_optional:
        ds.StudyDate = "20240301"
        ds.StudyTime = "101500"
        ds.StudyID = "STUDY1"
        ds.SeriesNumber = "1"
        ds.InstanceNumber = "1"
    for keyword in drop:
        if keyword == "TransferSyntaxUID":
            del ds.file_meta.TransferSyntaxUID
        elif keyword in ds:
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


def _download(client, job_id: str):
    return client.get(f"/jobs/{job_id}/dicomdir/download")


def _extract(content: bytes, dest):
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        zf.extractall(dest)
        return set(zf.namelist())


def _registry():
    from app.main import registry
    return registry


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
def test_dicomdir_basic_hierarchy_and_headers(client):
    files = {
        "a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        "a2.dcm": _ct_bytes("1.1.2", "2.1.1", "3.1"),
        "b1.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2",
                            patient_id="P-2", patient_name="Li^Si"),
    }
    job = _upload(client, files)
    r = _download(client, job["id"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["x-dicomdir-file-count"] == "3"
    assert r.headers["x-dicomdir-patient-count"] == "2"
    assert r.headers["x-dicomdir-study-count"] == "2"
    assert r.headers["x-dicomdir-series-count"] == "2"
    assert r.headers["content-disposition"].startswith(
        "attachment; filename=\"dicomdir-"
    )

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert "DICOMDIR" in names
    # 实例全部是符合 File ID 约束的 8 字符大写短组件
    instance_ids = [n for n in names if n != "DICOMDIR"]
    assert len(instance_ids) == 3
    for n in instance_ids:
        parts = n.split("/")
        assert len(parts) == 4
        assert all(len(p) == 8 and p == p.upper() and p.isalnum() for p in parts)


def test_dicomdir_records_reference_actual_files_and_anonymous_uids(client, tmp_path):
    files = {
        "a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        "a2.dcm": _ct_bytes("1.1.2", "2.1.1", "3.1"),
    }
    job = _upload(client, files)
    r = _download(client, job["id"])
    out = tmp_path / "fs"
    names = _extract(r.content, out)

    # pydicom 自身必须能重新加载该 DICOMDIR 并解析全部实例
    from pydicom.fileset import FileSet
    fs = FileSet(str(out / "DICOMDIR"))
    assert len(fs) == 2
    sops = set()
    for inst in fs:
        file_id = [str(p) for p in inst.ReferencedFileID]
        rel = "/".join(file_id)
        assert rel in names
        assert (out / rel).is_file()
        assert inst.StudyInstanceUID == derive_uid(SECRET, "3.1")
        assert inst.SeriesInstanceUID == derive_uid(SECRET, "2.1.1")
        sops.add(inst.SOPInstanceUID)
    assert sops == {derive_uid(SECRET, "1.1.1"), derive_uid(SECRET, "1.1.2")}

    # 患者记录必须是匿名伪名（取自匿名输出文件），不是原始身份
    pid = str(next(iter(fs)).PatientID)
    assert pid and pid != "P-1"
    pname = str(next(iter(fs)).PatientName)
    assert "Zhang" not in pname


def test_dicomdir_no_original_paths_or_phi_in_zip(client):
    files = {
        "secret_dir/patient_1/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
    }
    job = _upload(client, files)
    r = _download(client, job["id"])
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        for info in zf.infolist():
            assert "secret_dir" not in info.filename
            assert "patient_1" not in info.filename
            blob = zf.read(info.filename).decode("latin-1", errors="ignore")
            assert "Zhang" not in blob
            assert "secret_dir" not in blob


def test_dicomdir_file_ids_are_stable_across_requests(client):
    files = {
        "a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        "a2.dcm": _ct_bytes("1.1.2", "2.1.1", "3.1"),
        "a3.dcm": _ct_bytes("1.1.3", "2.1.2", "3.1"),
    }
    job = _upload(client, files)

    def layout():
        r = _download(client, job["id"])
        assert r.status_code == 200, r.text
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            return {
                n: zf.getinfo(n).CRC
                for n in zf.namelist() if n != "DICOMDIR"
            }

    first = layout()
    second = layout()
    assert set(first) == set(second)  # 同名路径
    assert first == second            # 同内容（CRC 一致）


def test_dicomdir_works_without_optional_type2_tags(client):
    # 只有核心层级标签（无 StudyDate/StudyID/SeriesNumber/InstanceNumber）
    files = {"min.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", include_optional=False)}
    job = _upload(client, files)
    r = _download(client, job["id"])
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
    assert names[0] == "DICOMDIR" or "DICOMDIR" in names
    assert len(names) == 2


def test_dicomdir_uses_optional_fields_when_present(client, tmp_path):
    files = {"a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")}
    job = _upload(client, files)
    r = _download(client, job["id"])
    out = tmp_path / "fs2"
    _extract(r.content, out)
    import pydicom
    dd = pydicom.dcmread(str(out / "DICOMDIR"))
    by_type: dict[str, list] = {}
    for rec in dd.DirectoryRecordSequence:
        by_type.setdefault(rec.DirectoryRecordType, []).append(rec)
    assert by_type["STUDY"][0].StudyDate == "20240301"
    # StudyID 属 Annex E 清空标签：匿名文件中为空，目录记录不应伪造
    assert not str(getattr(by_type["STUDY"][0], "StudyID", "") or "").strip()
    assert by_type["SERIES"][0].Modality == "CT"
    assert by_type["IMAGE"][0].InstanceNumber == "1"


def test_dicomdir_byte_identical_to_full_download(client):
    files = {
        "a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        "b1.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2",
                            patient_id="P-2", patient_name="Li^Si"),
    }
    job = _upload(client, files)
    rd = _download(client, job["id"])
    full = client.get(f"/jobs/{job['id']}/download")
    assert rd.status_code == full.status_code == 200
    with zipfile.ZipFile(io.BytesIO(rd.content)) as za:
        instance_blobs = {
            n: za.read(n) for n in za.namelist() if n != "DICOMDIR"
        }
    with zipfile.ZipFile(io.BytesIO(full.content)) as zb:
        full_blobs = {n: zb.read(n) for n in zb.namelist()}
    assert set(instance_blobs.values()) == set(full_blobs.values())


def test_dicomdir_temp_zip_cleaned_up(client):
    files = {"a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")}
    job = _upload(client, files)
    assert _download(client, job["id"]).status_code == 200
    job_obj = _registry().get(job["id"])
    base = os.path.dirname(job_obj.result.output_dir)
    leftovers = [
        n for n in os.listdir(base)
        if n.startswith("dicomdir-") and n.endswith(".zip")
    ]
    assert leftovers == []


# --------------------------------------------------------------------------- #
# 错误路径
# --------------------------------------------------------------------------- #
def test_dicomdir_job_not_found_404(client):
    r = _download(client, "deadbeef")
    assert r.status_code == 404
    assert r.json()["detail"] == "任务不存在或已过期"


def test_dicomdir_failed_job_409(client):
    r = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": SECRET},
    )
    job = r.json()
    assert job["status"] == "failed"
    resp = _download(client, job["id"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "JOB_NOT_READY"


def test_dicomdir_unreadable_output_409(client):
    files = {"a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")}
    job = _upload(client, files)
    job_obj = _registry().get(job["id"])
    # 破坏其中一份匿名输出
    target = next(
        os.path.join(job_obj.result.output_dir, r.output_name)
        for r in job_obj.result.files if r.output_name
    )
    with open(target, "wb") as fh:
        fh.write(b"not a dicom anymore")

    resp = _download(client, job["id"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "DICOMDIR_BUILD_INCOMPLETE"
    problem = detail["problems"][0]
    assert problem["reason"] == "unreadable"
    assert problem["output_file"].endswith(".dcm")
    assert problem["message"]


def test_dicomdir_missing_required_tags_409(client):
    # 缺失 Modality：匿名引擎不会补 Modality，输出缺 SERIES 记录必需标签
    files = {"a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", drop=("Modality",))}
    job = _upload(client, files)
    resp = _download(client, job["id"])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "DICOMDIR_BUILD_INCOMPLETE"
    problem = detail["problems"][0]
    assert problem["reason"] == "missing_required_tags"
    assert "Modality" in problem["missing_tags"]
    assert problem["sop_instance_uid"] == derive_uid(SECRET, "1.1.1")


def test_dicomdir_missing_transfer_syntax_409(client):
    files = {
        "a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", drop=("TransferSyntaxUID",)),
    }
    job = _upload(client, files)
    resp = _download(client, job["id"])
    # 匿名化写出阶段会补齐 Explicit VR，但确认接口在缺 TS 场景不 500
    assert resp.status_code in (200, 409)
    if resp.status_code == 409:
        problem = resp.json()["detail"]["problems"][0]
        assert problem["reason"] == "missing_required_tags"
        assert "TransferSyntaxUID" in problem["missing_tags"]


def test_dicomdir_duplicate_sop_409_lists_both_files(client):
    dup = "1.5.5.777"
    files = {
        "dup_a.dcm": _ct_bytes(dup, "2.5.5", "3.55"),
        "dup_b.dcm": _ct_bytes(dup, "2.5.5", "3.55"),
    }
    job = _upload(client, files)
    resp = _download(client, job["id"])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "DICOMDIR_BUILD_INCOMPLETE"
    problem = next(
        p for p in detail["problems"]
        if p["sop_instance_uid"] == derive_uid(SECRET, dup)
    )
    assert problem["reason"] == "ambiguous_output"
    new_sop = derive_uid(SECRET, dup)
    assert problem["output_files"] == sorted(
        [f"{new_sop}.dcm", f"{new_sop}-2.dcm"]
    )
    # 残缺包绝不能下载
    assert not resp.headers.get("content-type", "").startswith("application/zip")


def test_dicomdir_collects_all_problem_files_at_once(client):
    # 一份缺 Modality、一份缺 Series UID：两个问题应一次性列出
    files = {
        "a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", drop=("Modality",)),
        "a2.dcm": _ct_bytes("1.1.2", "2.1.2", "3.1", drop=("Modality",)),
        "good.dcm": _ct_bytes("1.9.9", "2.9.9", "3.9",
                              patient_id="P-9", patient_name="Wang^Wu"),
    }
    job = _upload(client, files)
    resp = _download(client, job["id"])
    assert resp.status_code == 409
    problems = resp.json()["detail"]["problems"]
    assert {p["output_file"] for p in problems} == {
        derive_uid(SECRET, "1.1.1") + ".dcm",
        derive_uid(SECRET, "1.1.2") + ".dcm",
    }
    # 即使存在好文件，也不允许输出残缺目录
    assert all(p["reason"] == "missing_required_tags" for p in problems)


def test_dicomdir_full_download_still_works_when_directory_rejected(client):
    files = {"a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", drop=("Modality",))}
    job = _upload(client, files)
    assert _download(client, job["id"]).status_code == 409
    full = client.get(f"/jobs/{job['id']}/download")
    assert full.status_code == 200
    with zipfile.ZipFile(io.BytesIO(full.content)) as zf:
        assert len(zf.namelist()) == 1


def test_dicomdir_sample_zip_with_rtstruct(client, tmp_path):
    from tests.sample_data import build_sample_zip

    zip_path = tmp_path / "sample.zip"
    build_sample_zip(str(zip_path), seed=1)
    with open(zip_path, "rb") as fh:
        r = client.post(
            "/jobs",
            files={"file": ("study.zip", fh, "application/zip")},
            data={"secret": SECRET, "shift_dates": "false"},
        )
    assert r.status_code == 201, r.text
    job = r.json()

    resp = _download(client, job["id"])
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-dicomdir-file-count"] == "4"
    out = tmp_path / "sample-fs"
    names = _extract(resp.content, out)

    from pydicom.fileset import FileSet
    fs = FileSet(str(out / "DICOMDIR"))
    rels = {
        "/".join(str(p) for p in inst.ReferencedFileID) for inst in fs
    }
    assert rels <= names
    leaf_types = {
        rec.DirectoryRecordType
        for rec in pydicom_dcmread(out / "DICOMDIR").DirectoryRecordSequence
    }
    assert "RT STRUCTURE SET" in leaf_types
    assert "PATIENT" in leaf_types and "STUDY" in leaf_types and "SERIES" in leaf_types


def pydicom_dcmread(path):
    import pydicom
    return pydicom.dcmread(str(path))
