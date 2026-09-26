"""匿名化引擎测试：UID 一致性、PHI 清除、非法文件诊断、日期平移、ZIP 安全。"""
from __future__ import annotations

import io
import os
import zipfile

import pydicom
import pytest

from app.anonymizer import Anonymizer, build_zip, safe_extract_zip
from app.config import settings

from .sample_data import _ct_like, build_sample_zip

SECRET = "unit-test-secret-0123456789"


@pytest.fixture()
def processed(tmp_path):
    src_zip = tmp_path / "in.zip"
    meta = build_sample_zip(str(src_zip))
    ex = tmp_path / "extracted"
    ex.mkdir()
    members = safe_extract_zip(
        str(src_zip), str(ex),
        settings.max_extracted_bytes, settings.max_extracted_files,
    )
    out = tmp_path / "out"
    engine = Anonymizer(secret=SECRET, shift_dates=True)
    result = engine.run(str(ex), members, str(out))
    return tmp_path, result, meta


def test_invalid_files_are_reported_per_file(processed):
    _, result, _ = processed
    by_name = {r.name: r for r in result.files}
    assert by_name["broken/bad.dcm"].status == "error"
    codes = [d.code for d in by_name["broken/bad.dcm"].diagnostics]
    assert "INVALID_DICOM" in codes
    assert by_name["notes/not-a-dicom.txt"].status == "error"


def test_valid_files_are_ok_and_written(processed):
    _, result, _ = processed
    valid = [r for r in result.files if r.name.startswith("patient_")]
    assert len(valid) == 4
    assert all(r.status in ("ok", "warning") for r in valid)
    assert all(r.output_name and r.output_name.endswith(".dcm") for r in valid)


def test_patient_pseudonym_shared_across_files(processed):
    _, result, _ = processed
    p1_files = [r for r in result.files if r.name.startswith("patient_1/")]
    ids = {
        next(
            c.new for c in r.changes
            if c.keyword == "PatientID" and c.path in ("", "/")
        )
        for r in p1_files
    }
    assert len(ids) == 1  # 同患者三文件伪名一致


def test_study_relationship_preserved(processed):
    _, result, _ = processed
    p1 = [r for r in result.files if r.name.startswith("patient_1/")]
    study_uids = {r.study_instance_uid for r in p1}
    assert len(study_uids) == 1
    # 不同系列（CT vs RTSTRUCT 各自的 series）
    series_uids = {r.series_instance_uid for r in p1}
    assert len(series_uids) == 2


def test_nested_references_remapped_consistently(processed):
    tmp, result, meta = processed
    rt = next(r for r in result.files if r.name.endswith("RS0001.dcm"))
    ct1 = next(
        r for r in result.files
        if r.name == "patient_1/CT0001.dcm"
    )
    ds = pydicom.dcmread(os.path.join(result.output_dir, rt.output_name))
    nested = (
        ds.ReferencedFrameOfReferenceSequence[0]
        .RTReferencedStudySequence[0]
        .ContourImageSequence[0]
    )
    assert nested.ReferencedSOPInstanceUID == ct1.sop_instance_uid
    # file meta 与顶层 SOP 一致
    assert ds.file_meta.MediaStorageSOPInstanceUID == ds.SOPInstanceUID
    assert ds.StudyInstanceUID == ct1.study_instance_uid


def test_dangling_reference_is_warning(processed):
    _, result, meta = processed
    rt = next(r for r in result.files if r.name.endswith("RS0001.dcm"))
    codes = [d.code for d in rt.diagnostics]
    assert "DANGLING_REFERENCE" in codes
    # 悬空引用仍被确定性重写
    ds = pydicom.dcmread(os.path.join(result.output_dir, rt.output_name))
    new_dangling = ds.ReferencedSOPSequence[0].ReferencedSOPInstanceUID
    assert new_dangling != meta["dangling"]
    assert new_dangling.startswith("2.25.")


def test_phi_fields_removed(processed):
    _, result, _ = processed
    ct = next(r for r in result.files if r.name == "patient_1/CT0001.dcm")
    ds = pydicom.dcmread(os.path.join(result.output_dir, ct.output_name))
    assert len(ds.PatientID) == 16
    assert str(ds.PatientName).startswith("ANON^")
    assert ds.AccessionNumber == ""
    assert ds.InstitutionName == ""
    assert not hasattr(ds, "PatientAddress")
    assert not hasattr(ds, "ImageComments")
    assert not [e for e in ds if e.tag.is_private]
    assert ds.PatientIdentityRemoved == "YES"
    assert ds.DeidentificationMethod
    # 像素数据保持不变
    assert ds.PixelData == bytes(range(32))


def test_change_preview_records_replacements(processed):
    _, result, _ = processed
    ct = next(r for r in result.files if r.name == "patient_1/CT0001.dcm")
    keywords = {c.keyword for c in ct.changes}
    assert {"PatientID", "PatientName", "StudyInstanceUID", "SeriesInstanceUID",
            "SOPInstanceUID"} <= keywords
    # 审计标签记录为新增 REPLACE
    assert "PatientIdentityRemoved" in keywords
    assert "DeidentificationMethod" in keywords
    for c in ct.changes:
        assert c.action in ("REPLACE", "EMPTY", "REMOVE")
        assert c.new is not None or c.action in ("EMPTY", "REMOVE")


def test_dates_are_shifted_and_stable(processed):
    _, result, _ = processed
    ct = next(r for r in result.files if r.name == "patient_1/CT0001.dcm")
    ds = pydicom.dcmread(os.path.join(result.output_dir, ct.output_name))
    assert ds.PatientBirthDate != "19800115"
    assert ds.StudyDate != "20240301"
    # 患者出生日期平移与检查日期使用同一患者偏移（-103 天来自该密钥派生）
    pinfo = next(p for p in result.patients.values()
                 if p.new_patient_id == ds.PatientID)
    assert pinfo.date_shift_days != 0


def test_determinism_across_runs(tmp_path):
    """同密钥、同原始 UID → 同新 UID（患者级稳定性）。"""
    zp = tmp_path / "fixed.zip"
    build_sample_zip(str(zp), seed=42)
    ex = tmp_path / "base"
    ex.mkdir()
    members = safe_extract_zip(str(zp), str(ex),
                               settings.max_extracted_bytes,
                               settings.max_extracted_files)

    def run(secret, tag):
        out = tmp_path / tag
        return Anonymizer(secret=secret).run(str(ex), list(members), str(out))

    r1 = run(SECRET, "out-a")
    r2 = run(SECRET, "out-b")
    r3 = run("another-secret-987654321", "out-c")
    sops1 = {r.name: r.sop_instance_uid for r in r1.files if r.output_name}
    sops2 = {r.name: r.sop_instance_uid for r in r2.files if r.output_name}
    sops3 = {r.name: r.sop_instance_uid for r in r3.files if r.output_name}
    assert sops1 == sops2
    assert sops1["patient_1/CT0001.dcm"] != sops3["patient_1/CT0001.dcm"]


def test_output_zip_flat_and_readable(processed):
    tmp, result, _ = processed
    zpath = build_zip(result, str(tmp / "out.zip"))
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        assert len(names) == 4
        assert all("/" not in n and n.endswith(".dcm") for n in names)
        with zf.open(names[0]) as fh:
            pydicom.dcmread(io.BytesIO(fh.read()))  # 输出可被标准解析器读取


def test_shift_dates_disabled_keeps_dates(tmp_path):
    zp = tmp_path / "in.zip"
    build_sample_zip(str(zp))
    ex = tmp_path / "ex"; ex.mkdir()
    members = safe_extract_zip(str(zp), str(ex),
                               settings.max_extracted_bytes,
                               settings.max_extracted_files)
    result = Anonymizer(secret=SECRET, shift_dates=False).run(
        str(ex), members, str(tmp_path / "out"))
    ct = next(r for r in result.files if r.name == "patient_1/CT0001.dcm")
    ds = pydicom.dcmread(os.path.join(result.output_dir, ct.output_name))
    assert ds.StudyDate == "20240301"
    assert ds.PatientBirthDate == ""  # 出生日期不平移则清空


def _extract(tmp_path, tag="ex"):
    zp = tmp_path / f"{tag}.zip"
    build_sample_zip(str(zp))
    ex = tmp_path / tag
    ex.mkdir()
    members = safe_extract_zip(str(zp), str(ex),
                               settings.max_extracted_bytes,
                               settings.max_extracted_files)
    return str(ex), members


def test_custom_shift_range_bounded_and_deterministic(tmp_path):
    """自定义闭区间：偏移落在区间内，且跨批次（两次独立处理）完全一致。"""
    ex1, m1 = _extract(tmp_path, "ex1")
    ex2, m2 = _extract(tmp_path, "ex2")
    r1 = Anonymizer(
        secret=SECRET, shift_dates=True, date_shift_min=-5, date_shift_max=-5
    ).run(ex1, m1, str(tmp_path / "o1"))
    r2 = Anonymizer(
        secret=SECRET, shift_dates=True, date_shift_min=-5, date_shift_max=-5
    ).run(ex2, m2, str(tmp_path / "o2"))

    shifts1 = {p.key: p.date_shift_days for p in r1.patients.values()}
    shifts2 = {p.key: p.date_shift_days for p in r2.patients.values()}
    assert shifts1 == shifts2  # 跨批次、范围相同 -> 一致
    assert set(shifts1.values()) == {-5}

    ct = next(r for r in r1.files if r.name == "patient_1/CT0001.dcm")
    ds = pydicom.dcmread(os.path.join(r1.output_dir, ct.output_name))
    # 2024-03-01 前移 5 天 -> 2024-02-25
    assert ds.StudyDate == "20240225"
    # 出生日期与检查日期使用同一患者偏移
    assert ds.PatientBirthDate == "19800110"


def test_shift_disabled_ignores_range_and_uses_zero(tmp_path):
    """关闭平移时即使携带范围，偏移仍为 0，DA 保持原值、出生日期清空。"""
    ex, members = _extract(tmp_path, "exoff")
    result = Anonymizer(
        secret=SECRET, shift_dates=False, date_shift_min=-10, date_shift_max=10
    ).run(ex, members, str(tmp_path / "outoff"))
    assert {p.date_shift_days for p in result.patients.values()} == {0}
    ct = next(r for r in result.files if r.name == "patient_1/CT0001.dcm")
    ds = pydicom.dcmread(os.path.join(result.output_dir, ct.output_name))
    assert ds.StudyDate == "20240301"
    assert ds.PatientBirthDate == ""


def test_default_range_matches_pre_range_version(tmp_path):
    """缺省区间下的偏移与未引入范围参数前的 v1 派生完全相同。"""
    from app import crypto

    ex, members = _extract(tmp_path, "exdef")
    result = Anonymizer(secret=SECRET, shift_dates=True).run(
        ex, members, str(tmp_path / "outdef"))
    for p in result.patients.values():
        assert p.date_shift_days == crypto.date_shift_days(SECRET, p.key)


def test_missing_identity_elements_get_pseudonym(tmp_path):
    """输入文件完全没有 PatientID/PatientName 时，输出仍被赋予患者伪名。"""
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import MRImageStorage, ExplicitVRLittleEndian

    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = MRImageStorage
    fm.MediaStorageSOPInstanceUID = "1.2.3.9.1"
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=fm, preamble=b"\0" * 128)
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = "1.2.3.9.1"
    ds.StudyInstanceUID = "1.2.3.99"
    ds.SeriesInstanceUID = "1.2.3.999"
    ds.Modality = "MR"
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)

    zp = tmp_path / "noid.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("x.dcm", buf.getvalue())
    ex = tmp_path / "ex"
    ex.mkdir()
    members = safe_extract_zip(str(zp), str(ex),
                               settings.max_extracted_bytes,
                               settings.max_extracted_files)
    result = Anonymizer(secret=SECRET).run(str(ex), members, str(tmp_path / "out"))
    rep = result.files[0]
    assert rep.status == "warning"
    out = pydicom.dcmread(os.path.join(result.output_dir, rep.output_name))
    assert len(out.PatientID) == 16
    assert str(out.PatientName).startswith("ANON^")
    pid_changes = [c for c in rep.changes if c.keyword == "PatientID"]
    assert len(pid_changes) == 1  # 不重复记录


# --------------------------------------------------------------------------- #
# 患者身份归组：IssuerOfPatientID + PatientID
# --------------------------------------------------------------------------- #
def _issuer_ct(sop_uid: str, pid: str, issuer: str | None) -> bytes:
    return _ct_like(sop_uid, f"{sop_uid}.2", f"{sop_uid}.3", pid,
                    "Doe^X", f"{sop_uid}.4", issuer=issuer)


def test_patient_identity_key_uses_issuer_when_present():
    from pydicom.dataset import Dataset

    key = Anonymizer._patient_identity
    ds = Dataset()
    ds.PatientID = "P-9"
    ds.IssuerOfPatientID = "HOSP-A"
    assert key(ds, "/unused")[0] == "id:HOSP-A\x1fP-9"

    ds = Dataset()
    ds.PatientID = "P-9"
    assert key(ds, "/unused")[0] == "id:P-9"  # 无签发机构：旧键不变

    ds = Dataset()
    ds.PatientID = "P-9"
    ds.IssuerOfPatientID = "   "
    assert key(ds, "/unused")[0] == "id:P-9"  # 空白签发机构等同缺失

    ds = Dataset()
    ds.PatientName = "Doe^John"
    assert key(ds, "/unused")[0] == "name:DOE^JOHN"


def test_same_pid_different_issuer_groups_separately(tmp_path):
    """同 PatientID 在不同签发机构（含无签发机构）下是不同患者。"""
    members_spec = [
        ("a.dcm", _issuer_ct("9.1.1", "P-9", "HOSP-A")),
        ("d.dcm", _issuer_ct("9.1.4", "P-9", "HOSP-A")),  # 同机构同号 -> 与 a 同组
        ("b.dcm", _issuer_ct("9.1.2", "P-9", "HOSP-B")),
        ("c.dcm", _issuer_ct("9.1.3", "P-9", None)),      # 无机构 -> 旧 id: 键
    ]
    zp = tmp_path / "iss.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for name, blob in members_spec:
            zf.writestr(name, blob)
    ex = tmp_path / "ex"
    ex.mkdir()
    members = safe_extract_zip(str(zp), str(ex),
                               settings.max_extracted_bytes,
                               settings.max_extracted_files)
    result = Anonymizer(secret=SECRET).run(str(ex), members, str(tmp_path / "out"))

    key_a, key_b, key_bare = "id:HOSP-A\x1fP-9", "id:HOSP-B\x1fP-9", "id:P-9"
    assert set(result.patients) == {key_a, key_b, key_bare}
    by_name = {r.name: r for r in result.files}
    assert by_name["a.dcm"].patient_key == key_a
    assert by_name["d.dcm"].patient_key == key_a
    assert by_name["b.dcm"].patient_key == key_b
    assert by_name["c.dcm"].patient_key == key_bare

    pa, pb, pc = (result.patients[k] for k in (key_a, key_b, key_bare))
    assert pa.file_count == 2  # 同机构同号两文件归为同一患者
    assert pb.file_count == 1 and pc.file_count == 1
    # 三个身份各自独立的伪名；同组两文件共用伪名
    assert len({pa.new_patient_id, pb.new_patient_id, pc.new_patient_id}) == 3

    # 每个输出文件的伪名与其身份键一致
    for name, pinfo in (("a.dcm", pa), ("b.dcm", pb), ("c.dcm", pc), ("d.dcm", pa)):
        ds = pydicom.dcmread(
            os.path.join(result.output_dir, by_name[name].output_name))
        assert ds.PatientID == pinfo.new_patient_id
        assert str(ds.PatientName) == pinfo.new_patient_name
        # 原签发机构不得留在匿名 DICOM 中
        assert not hasattr(ds, "IssuerOfPatientID")

    # 变更预览中 IssuerOfPatientID 记录为 REMOVE
    issuer_changes = [
        c for r in result.files for c in r.changes
        if c.keyword == "IssuerOfPatientID"
    ]
    assert issuer_changes and all(c.action == "REMOVE" for c in issuer_changes)


def test_issuer_identity_stable_across_batches(tmp_path):
    """同一签发机构 + 患者号在两个独立批次中归为同一身份键、派生出相同结果。"""
    def run_batch(tag: str):
        zp = tmp_path / f"{tag}.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("a.dcm", _issuer_ct("9.2.1", "P-9", "HOSP-A"))
        ex = tmp_path / f"{tag}-ex"
        ex.mkdir()
        members = safe_extract_zip(str(zp), str(ex),
                                   settings.max_extracted_bytes,
                                   settings.max_extracted_files)
        return Anonymizer(secret=SECRET).run(
            str(ex), members, str(tmp_path / f"{tag}-out"))

    r1, r2 = run_batch("b1"), run_batch("b2")
    assert set(r1.patients) == set(r2.patients) == {"id:HOSP-A\x1fP-9"}
    p1, p2 = r1.patients["id:HOSP-A\x1fP-9"], r2.patients["id:HOSP-A\x1fP-9"]
    assert p1.new_patient_id == p2.new_patient_id
    assert p1.new_patient_name == p2.new_patient_name
    assert p1.date_shift_days == p2.date_shift_days


def test_empty_issuer_keeps_legacy_identity_and_derivation(tmp_path):
    """签发机构为空时保留旧 id: 键，伪名与日期偏移与升级前逐字节一致。"""
    from app import crypto

    zp = tmp_path / "bare.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("a.dcm", _issuer_ct("9.3.1", "P-001", None))
    ex = tmp_path / "ex"
    ex.mkdir()
    members = safe_extract_zip(str(zp), str(ex),
                               settings.max_extracted_bytes,
                               settings.max_extracted_files)
    result = Anonymizer(secret=SECRET, shift_dates=True).run(
        str(ex), members, str(tmp_path / "out"))
    assert set(result.patients) == {"id:P-001"}
    pinfo = result.patients["id:P-001"]
    legacy_id, legacy_name = crypto.patient_pseudonyms(SECRET, "id:P-001")
    assert pinfo.new_patient_id == legacy_id
    assert pinfo.new_patient_name == legacy_name
    assert pinfo.date_shift_days == crypto.date_shift_days(SECRET, "id:P-001")

    # 带签发机构的键不应与旧键派生出同一伪名
    ex2 = tmp_path / "ex2"
    ex2.mkdir()
    zp2 = tmp_path / "scoped.zip"
    with zipfile.ZipFile(zp2, "w") as zf:
        zf.writestr("a.dcm", _issuer_ct("9.3.2", "P-001", "HOSP-A"))
    members2 = safe_extract_zip(str(zp2), str(ex2),
                                settings.max_extracted_bytes,
                                settings.max_extracted_files)
    r2 = Anonymizer(secret=SECRET).run(
        str(ex2), members2, str(tmp_path / "out2"))
    scoped = r2.patients["id:HOSP-A\x1fP-001"]
    assert scoped.new_patient_id != legacy_id


# --------------------------------------------------------------------------- #
# ZIP 安全
# --------------------------------------------------------------------------- #
def test_zip_path_traversal_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../etc/passwd", b"x")
    dest = tmp_path / "ex"
    dest.mkdir()
    with pytest.raises(ValueError):
        safe_extract_zip(str(evil), str(dest),
                         settings.max_extracted_bytes,
                         settings.max_extracted_files)


def test_zip_bomb_size_rejected(tmp_path):
    big = tmp_path / "big.zip"
    with zipfile.ZipFile(big, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.dcm", b"0" * 1024)
    dest = tmp_path / "ex"
    dest.mkdir()
    with pytest.raises(ValueError):
        safe_extract_zip(str(big), str(dest), max_total_bytes=512, max_files=100)


def test_too_many_files_rejected(tmp_path):
    many = tmp_path / "many.zip"
    with zipfile.ZipFile(many, "w") as zf:
        for i in range(5):
            zf.writestr(f"f{i}", b"x")
    dest = tmp_path / "ex"
    dest.mkdir()
    with pytest.raises(ValueError):
        safe_extract_zip(str(many), str(dest),
                         settings.max_extracted_bytes, max_files=3)


def test_zip_absolute_path_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("/etc/passwd", b"x")
    dest = tmp_path / "ex"
    dest.mkdir()
    with pytest.raises(ValueError):
        safe_extract_zip(str(evil), str(dest),
                         settings.max_extracted_bytes,
                         settings.max_extracted_files)


def test_zip_symlink_rejected(tmp_path):
    link = tmp_path / "link.zip"
    with zipfile.ZipFile(link, "w") as zf:
        info = zipfile.ZipInfo("link.dcm")
        info.external_attr = 0o120777 << 16  # Unix 文件类型：符号链接
        zf.writestr(info, b"/etc/passwd")
    dest = tmp_path / "ex"
    dest.mkdir()
    with pytest.raises(ValueError):
        safe_extract_zip(str(link), str(dest),
                         settings.max_extracted_bytes,
                         settings.max_extracted_files)


# --------------------------------------------------------------------------- #
# ZIP 同名条目：逐条目独立暂存，两遍扫描与写出均读各自字节
# --------------------------------------------------------------------------- #
def _extract_members(tmp_path, members, tag="dup"):
    """把 (成员名, 字节) 列表原样写入 ZIP（允许同名条目）并安全解压。"""
    zp = tmp_path / f"{tag}.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for name, blob in members:
            zf.writestr(name, blob)
    ex = tmp_path / f"{tag}-ex"
    ex.mkdir()
    entries = safe_extract_zip(str(zp), str(ex),
                               settings.max_extracted_bytes,
                               settings.max_extracted_files)
    return str(ex), entries


def _ct(sop_uid, pixels):
    return _ct_like(sop_uid, "1.9.9.2", "1.9.9.1", "P-001", "Zhang^San",
                    "1.9.9.3", pixels=pixels)


def test_same_name_entries_extract_to_independent_staging(tmp_path):
    ex, entries = _extract_members(
        tmp_path, [("scan.dcm", b"first"), ("scan.dcm", b"second")]
    )
    assert [e.index for e in entries] == [0, 1]
    assert [e.name for e in entries] == ["scan.dcm", "scan.dcm"]
    assert entries[0].staging != entries[1].staging
    for entry, expect in zip(entries, (b"first", b"second")):
        with open(os.path.join(ex, entry.staging), "rb") as fh:
            assert fh.read() == expect  # 后一条目不覆盖前一条目


def test_same_name_different_sop_each_written(tmp_path):
    pixels_a = bytes(range(32))
    pixels_b = bytes(reversed(range(32)))
    ex, entries = _extract_members(
        tmp_path,
        [("scan.dcm", _ct("1.2.3.4.5.6.7.8.9.1.101", pixels_a)),
         ("scan.dcm", _ct("1.2.3.4.5.6.7.8.9.1.102", pixels_b))],
    )
    result = Anonymizer(secret=SECRET).run(ex, entries, str(tmp_path / "out"))

    assert len(result.files) == 2
    assert [r.name for r in result.files] == ["scan.dcm", "scan.dcm"]
    assert {r.entry_index for r in result.files} == {0, 1}
    assert all(r.output_name for r in result.files)
    # 同名不同 SOP：各自写出为不同的匿名文件
    new_sops = {r.sop_instance_uid for r in result.files}
    assert len(new_sops) == 2
    assert {r.output_name for r in result.files} == {f"{u}.dcm" for u in new_sops}
    # 两个原始 SOP 都进入映射表（第一遍按条目各自收集）
    assert set(result.sops) == {"1.2.3.4.5.6.7.8.9.1.101",
                                "1.2.3.4.5.6.7.8.9.1.102"}
    # 输出像素与各自输入一致（第二遍读取各自暂存字节，而非末项重复）
    pixels_by_index = {}
    for r in result.files:
        ds = pydicom.dcmread(os.path.join(result.output_dir, r.output_name))
        pixels_by_index[r.entry_index] = ds.PixelData
    assert pixels_by_index[0] == pixels_a
    assert pixels_by_index[1] == pixels_b
    # 两名条目属于同一患者，只归为一组
    assert len(result.patients) == 1
    assert next(iter(result.patients.values())).file_count == 2


def test_normalized_same_address_entries_stay_independent(tmp_path):
    """`./scan.dcm` 与 `scan.dcm` 规范化后同址，但仍按条目各自独立处理。"""
    ex, entries = _extract_members(
        tmp_path,
        [("./scan.dcm", _ct("1.2.3.4.5.6.7.8.9.1.101", bytes(range(32)))),
         ("scan.dcm", _ct("1.2.3.4.5.6.7.8.9.1.102", bytes(range(32))))],
    )
    result = Anonymizer(secret=SECRET).run(ex, entries, str(tmp_path / "out"))
    # 原成员名原样回显，条目序号可区分
    assert [r.name for r in result.files] == ["./scan.dcm", "scan.dcm"]
    assert {r.entry_index for r in result.files} == {0, 1}
    assert len({r.sop_instance_uid for r in result.files}) == 2
    assert all(r.output_name for r in result.files)


def test_same_name_same_sop_uses_duplicate_rule(tmp_path):
    """同名且同 SOPInstanceUID：沿用现有重复实例规则（改名 + 警告）。"""
    blob = _ct("1.2.3.4.5.6.7.8.9.1.101", bytes(range(32)))
    ex, entries = _extract_members(
        tmp_path, [("scan.dcm", blob), ("scan.dcm", blob)]
    )
    result = Anonymizer(secret=SECRET).run(ex, entries, str(tmp_path / "out"))

    assert len(result.files) == 2
    new_sop = result.files[0].sop_instance_uid
    assert result.files[1].sop_instance_uid == new_sop
    outputs = sorted(r.output_name for r in result.files)
    assert outputs == [f"{new_sop}-2.dcm", f"{new_sop}.dcm"]
    dup = result.files[1]
    assert any(d.code == "DUPLICATE_SOP_INSTANCE" for d in dup.diagnostics)
    assert dup.status == "warning"


# --------------------------------------------------------------------------- #
# 顶层 UID 标签存在但值为空（与标签缺失同口径处理）
# --------------------------------------------------------------------------- #
def _uid_case_dataset(sop: "str | None", study: "str | None",
                      series: "str | None", *, ref_sop: "str | None" = None,
                      media_sop: "str | None" = None) -> bytes:
    """构造最小 DICOM。

    None 表示**不写入标签**（标签缺失）；"" 表示**写入但值为空**；
    其余字符串为正常 UID。用于覆盖“标签存在但值为空”这一历史误判边界。
    """
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = media_sop if media_sop is not None else "9.0.0.1"
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=fm, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    if sop is not None:
        ds.SOPInstanceUID = sop
    if study is not None:
        ds.StudyInstanceUID = study
    if series is not None:
        ds.SeriesInstanceUID = series
    ds.PatientID = "P-1"
    ds.PatientName = "Doe^X"
    if ref_sop is not None:
        item = Dataset()
        item.ReferencedSOPClassUID = CTImageStorage
        item.ReferencedSOPInstanceUID = ref_sop
        ds.ReferencedSOPSequence = [item]
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def _run_uids(tmp_path, members: dict, tag: str = "uids"):
    zp = tmp_path / f"{tag}.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for name, blob in members.items():
            zf.writestr(name, blob)
    ex = tmp_path / f"{tag}-ex"
    ex.mkdir()
    entries = safe_extract_zip(str(zp), str(ex),
                               settings.max_extracted_bytes,
                               settings.max_extracted_files)
    return Anonymizer(secret=SECRET).run(
        str(ex), entries, str(tmp_path / f"{tag}-out"))


def test_present_but_empty_uids_are_filled_like_absent(tmp_path):
    """标签存在但值为空与标签缺失走同一确定性补齐路径，不再 WRITE_ERROR/空 UID。"""
    result = _run_uids(tmp_path, {
        "all_empty.dcm": _uid_case_dataset("", "", ""),
        "all_absent.dcm": _uid_case_dataset(None, None, None),
        "sop_empty.dcm": _uid_case_dataset("", "9.1.2", "9.1.3"),
        "study_series_empty.dcm": _uid_case_dataset("9.2.1", "", ""),
    })

    expect_codes = {
        "all_empty.dcm": {"MISSING_STUDY_UID", "MISSING_SERIES_UID", "MISSING_SOP_UID"},
        "all_absent.dcm": {"MISSING_STUDY_UID", "MISSING_SERIES_UID", "MISSING_SOP_UID"},
        "sop_empty.dcm": {"MISSING_SOP_UID"},
        "study_series_empty.dcm": {"MISSING_STUDY_UID", "MISSING_SERIES_UID"},
    }
    by_name = {r.name: r for r in result.files}
    for name, codes in expect_codes.items():
        rep = by_name[name]
        # 历史缺陷：空 SOP -> WRITE_ERROR；空 Study/Series -> 输出空 UID
        assert rep.status == "warning", name
        assert not any(d.code == "WRITE_ERROR" for d in rep.diagnostics), name
        assert codes <= {d.code for d in rep.diagnostics}, name

        out = pydicom.dcmread(os.path.join(result.output_dir, rep.output_name))
        top = (str(out.StudyInstanceUID).strip(),
               str(out.SeriesInstanceUID).strip(),
               str(out.SOPInstanceUID).strip())
        assert all(uid.startswith("2.25.") for uid in top), name
        # 顶层 / File Meta / 任务索引 / 下载文件名四处一致
        meta_sop = str(out.file_meta.MediaStorageSOPInstanceUID).strip()
        assert meta_sop == top[2] == rep.sop_instance_uid, name
        assert top[0] == rep.study_instance_uid, name
        assert top[1] == rep.series_instance_uid, name
        assert rep.output_name == f"{top[2]}.dcm", name
        assert top[0] in result.studies and top[1] in result.series, name


def test_empty_uid_fill_is_deterministic_per_file(tmp_path):
    """同一输入包二次处理，空值补齐的 UID 与输出名完全一致（确定性规则）。"""
    members = {"a.dcm": _uid_case_dataset("", "", ""),
               "b.dcm": _uid_case_dataset(None, None, None)}
    r1 = _run_uids(tmp_path, members, "run1")
    r2 = _run_uids(tmp_path, members, "run2")
    key = lambda r: sorted(
        (x.name, x.study_instance_uid, x.series_instance_uid,
         x.sop_instance_uid, x.output_name) for x in r.files)
    assert key(r1) == key(r2)
    # 两个不同文件即便三标签全缺/全空，也各自得到独立 UID（种子含文件哈希）
    uids1 = {x.sop_instance_uid for x in r1.files}
    assert len(uids1) == 2


def test_mixed_empty_and_normal_uids_keep_normal_mapping(tmp_path):
    """混合文件：正常 UID 的原有映射与嵌套引用不受空值补齐影响。"""
    from app import crypto

    result = _run_uids(tmp_path, {
        # Study/Series 为空但 SOP 正常，且嵌套引用同批正常实例
        "a_mixed.dcm": _uid_case_dataset(
            "9.9.9.1", "", "", ref_sop="9.9.9.2"),
        "b_normal.dcm": _uid_case_dataset(
            "9.9.9.2", "9.9.9.10", "9.9.9.11"),
    })
    by_name = {r.name: r for r in result.files}
    a, b = by_name["a_mixed.dcm"], by_name["b_normal.dcm"]

    # 正常文件无任何 UID 缺失诊断，映射按既有确定性规则
    assert b.status == "ok"
    assert b.sop_instance_uid == crypto.derive_uid(SECRET, "9.9.9.2")
    assert b.study_instance_uid == crypto.derive_uid(SECRET, "9.9.9.10")
    assert b.series_instance_uid == crypto.derive_uid(SECRET, "9.9.9.11")
    assert result.sops["9.9.9.2"] == b.sop_instance_uid

    # 混合文件：正常 SOP 映射不变，空 Study/Series 逐文件诊断 + 补齐
    assert a.sop_instance_uid == crypto.derive_uid(SECRET, "9.9.9.1")
    assert {d.code for d in a.diagnostics} == {
        "MISSING_STUDY_UID", "MISSING_SERIES_UID"}
    out_a = pydicom.dcmread(os.path.join(result.output_dir, a.output_name))
    # 嵌套引用仍重写到被引用正常实例的新 SOP（同一张全局映射表）
    assert (out_a.ReferencedSOPSequence[0].ReferencedSOPInstanceUID
            == b.sop_instance_uid)
    assert a.output_name == f"{a.sop_instance_uid}.dcm"


# --------------------------------------------------------------------------- #
# 多值 DA/DT（pydicom MultiValue 与反斜杠分隔字符串）逐项平移
# --------------------------------------------------------------------------- #
TAG_CALIB_DATE = str(pydicom.tag.Tag(0x00181200))        # (0018, 1200)
TAG_CALIB_DATETIME = str(pydicom.tag.Tag(0x00181201))    # (0018, 1201)
TAG_ANATOMIC_REGION_SQ = 0x00082218


def _calibration_dataset(sop: str, *, da=None, dt=None, nested=False) -> bytes:
    """构造带多值 DA/DT 的最小 CT；nested=True 时再在 AnatomicRegionSequence
    内放一对多值 DA/DT，覆盖嵌套序列的同一规则。

    (0018,1200)/(0018,1201) 是校准日期/日期时间标签（DA/DT 多值 VR），引擎按
    VR 分发、不依赖关键字，因此断言统一用标签比较。
    """
    from pydicom.dataelem import DataElement
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = sop
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=fm, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = f"{sop}.2"
    ds.SeriesInstanceUID = f"{sop}.3"
    ds.PatientID = "P-MV"
    ds.PatientName = "Doe^Multi"
    ds.StudyDate = "20240301"
    if da is not None:
        ds[0x00181200] = DataElement(0x00181200, "DA", da)
    if dt is not None:
        ds[0x00181201] = DataElement(0x00181201, "DT", dt)
    if nested:
        item = Dataset()
        item[0x00181200] = DataElement(0x00181200, "DA", ["20240101", "20240630"])
        item[0x00181201] = DataElement(
            0x00181201, "DT",
            ["20240101120000", "20240630235959.123456-0500"],
        )
        ds[TAG_ANATOMIC_REGION_SQ] = DataElement(
            TAG_ANATOMIC_REGION_SQ, "SQ", [item]
        )
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def _run_calibration(tmp_path, files: dict, *, shift_dates: bool = True,
                     shift: int = -5):
    zp = tmp_path / "mv.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for name, blob in files.items():
            zf.writestr(name, blob)
    ex = tmp_path / "mv-ex"
    ex.mkdir()
    entries = safe_extract_zip(str(zp), str(ex),
                               settings.max_extracted_bytes,
                               settings.max_extracted_files)
    engine = Anonymizer(
        secret=SECRET, shift_dates=shift_dates,
        date_shift_min=shift, date_shift_max=shift,
    )
    result = engine.run(str(ex), entries, str(tmp_path / "mv-out"))
    return result


def test_multivalue_da_dt_shifted_item_by_item(tmp_path):
    """pydicom 多值 DA/DT：逐项按同一患者偏移平移，项数/顺序不变。"""
    result = _run_calibration(tmp_path, {
        "a.dcm": _calibration_dataset(
            "9.7.1.1",
            da=["20240301", "20240315"],
            dt=["20240301101500+0100", "20240315083000.5"],
        ),
    })
    rep = result.files[0]
    assert rep.status == "ok"
    out = pydicom.dcmread(os.path.join(result.output_dir, rep.output_name))
    da = out[0x00181200]
    assert list(da.value) == ["20240225", "20240310"]  # 两项都平移 5 天
    assert da.VM == 2
    dt = out[0x00181201]
    # DT 只改日期前缀，时间与时区/小数秒尾部原样保留
    assert list(dt.value) == ["20240225101500+0100", "20240310083000.5"]
    assert dt.VM == 2

    # 变更预览与写出内容一致（旧值/新值均以反斜杠拼接，而非 Python 列表展示）
    by_tag = {c.tag: c for c in rep.changes}
    assert by_tag[TAG_CALIB_DATE].old == "20240301\\20240315"
    assert by_tag[TAG_CALIB_DATE].new == "20240225\\20240310"
    assert by_tag[TAG_CALIB_DATETIME].new == (
        "20240225101500+0100\\20240310083000.5")
    for tag_ in (0x00181200, 0x00181201):
        ch = by_tag[str(pydicom.tag.Tag(tag_))]
        assert ch.new == "\\".join(str(v) for v in out[tag_].value)


def test_backslash_separated_da_dt_shifted_item_by_item(tmp_path):
    """VM=1 标签写入反斜杠分隔值时同样逐项处理，写出仍为同项数多值。"""
    result = _run_calibration(tmp_path, {
        "a.dcm": _calibration_dataset(
            "9.7.2.1",
            da="20240301\\20240315",
            dt="20240301101500\\20240315083000",
        ),
    })
    rep = result.files[0]
    assert rep.status == "ok"
    out = pydicom.dcmread(os.path.join(result.output_dir, rep.output_name))
    assert list(out[0x00181200].value) == ["20240225", "20240310"]
    assert list(out[0x00181201].value) == ["20240225101500", "20240310083000"]
    by_tag = {c.tag: c for c in rep.changes}
    assert by_tag[TAG_CALIB_DATE].new == "20240225\\20240310"


def test_multivalue_any_invalid_item_clears_whole_tag(tmp_path):
    """任一项日期非法：清空整项标签并记 INVALID_DATE，不残留首个/合法项。"""
    result = _run_calibration(tmp_path, {
        "bad_da.dcm": _calibration_dataset(
            "9.7.3.1", da=["20240301", "bogus"]),
        "bad_dt.dcm": _calibration_dataset(
            "9.7.3.2", dt=["20240301101500", "20240230"]),  # 2 月 30 日
    })
    by_name = {r.name: r for r in result.files}

    bad_da = by_name["bad_da.dcm"]
    assert bad_da.status == "warning"
    msg = next(d.message for d in bad_da.diagnostics
               if d.code == "INVALID_DATE")
    assert "第 2 项" in msg
    ch = next(c for c in bad_da.changes if c.tag == TAG_CALIB_DATE)
    assert ch.action == "EMPTY" and ch.new is None
    out_da = pydicom.dcmread(
        os.path.join(result.output_dir, bad_da.output_name))
    assert out_da[0x00181200].value == ""  # 合法首项也不得残留

    bad_dt = by_name["bad_dt.dcm"]
    assert any(d.code == "INVALID_DATE" for d in bad_dt.diagnostics)
    out_dt = pydicom.dcmread(
        os.path.join(result.output_dir, bad_dt.output_name))
    assert out_dt[0x00181201].value == ""


def test_nested_sequence_multivalue_da_dt_uses_same_rules(tmp_path):
    """嵌套序列中的多值 DA/DT 逐项平移，预览路径指向嵌套项。"""
    result = _run_calibration(tmp_path, {
        "a.dcm": _calibration_dataset("9.7.4.1", nested=True),
    })
    rep = result.files[0]
    assert rep.status == "ok"
    out = pydicom.dcmread(os.path.join(result.output_dir, rep.output_name))
    item = out[TAG_ANATOMIC_REGION_SQ][0]
    assert list(item[0x00181200].value) == ["20231227", "20240625"]
    assert list(item[0x00181201].value) == [
        "20231227120000", "20240625235959.123456-0500"]
    nested_changes = [
        c for c in rep.changes
        if c.tag in (TAG_CALIB_DATE, TAG_CALIB_DATETIME)
        and c.path not in ("", "/")
    ]
    assert len(nested_changes) == 2
    assert all("AnatomicRegionSequence[0]" in c.path for c in nested_changes)
    for c in nested_changes:
        g, e = c.tag.strip("()").split(",")
        nested_tag = pydicom.tag.Tag(int(g, 16), int(e, 16))
        assert c.new == "\\".join(
            str(v) for v in item[nested_tag].value)


def test_multivalue_dates_untouched_when_shift_disabled(tmp_path):
    """关闭日期平移：多值 DA/DT 维持原值，且不产生任何变更条目。"""
    result = _run_calibration(
        tmp_path,
        {"a.dcm": _calibration_dataset(
            "9.7.6.1",
            da=["20240301", "20240315"],
            dt=["20240301101500+0100", "20240315083000.5"],
        )},
        shift_dates=False,
    )
    rep = result.files[0]
    assert rep.status == "ok"
    out = pydicom.dcmread(os.path.join(result.output_dir, rep.output_name))
    assert list(out[0x00181200].value) == ["20240301", "20240315"]
    assert list(out[0x00181201].value) == [
        "20240301101500+0100", "20240315083000.5"]
    assert not [
        c for c in rep.changes
        if c.tag in (TAG_CALIB_DATE, TAG_CALIB_DATETIME)]


def test_multivalue_zero_delta_records_no_change(tmp_path):
    """派生偏移恰为 0 天时多值标签保持原样（不记录无意义变更）。"""
    result = _run_calibration(
        tmp_path,
        {"a.dcm": _calibration_dataset(
            "9.7.7.1", da=["20240301", "20240315"])},
        shift=0,
    )
    rep = result.files[0]
    out = pydicom.dcmread(os.path.join(result.output_dir, rep.output_name))
    assert list(out[0x00181200].value) == ["20240301", "20240315"]
    assert not [c for c in rep.changes if c.tag == TAG_CALIB_DATE]
