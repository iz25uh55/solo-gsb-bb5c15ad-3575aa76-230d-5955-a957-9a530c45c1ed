"""GET /jobs/{id}/instances/{sop_uid}/seg-coverage：匿名 BINARY SEG 分割覆盖率统计。"""
from __future__ import annotations

import io
import os
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEGBaseline8Bit,
    SegmentationStorage,
)

from app.crypto import derive_uid
from tests.sample_data import _write_bytes

SECRET = "seg-secret-123456"

STUDY = "4.1"
SERIES_CT = "4.2"
SERIES_SEG = "4.3"
CT1_SOP = "1.1.1"
CT2_SOP = "1.1.2"
SEG_SOP = "1.2.1"
SEG2_SOP = "1.2.2"

_ABSENT = object()  # 哨兵：不写入该元素
_AUTO = object()  # 哨兵：按帧数自动推导


# --------------------------------------------------------------------------- #
# 合成 DICOM：CT 源图像 + BINARY SEG
# --------------------------------------------------------------------------- #
def _ct_image_bytes(sop_uid: str, rows: int = 4, columns: int = 4,
                    series_uid: str = SERIES_CT) -> bytes:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = STUDY
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.Modality = "CT"
    ds.Rows = rows
    ds.Columns = columns
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = bytes(rows * columns * 2)
    return _write_bytes(ds)


def _fg_item(seg_num=_ABSENT, source_uid=_ABSENT) -> Dataset:
    """逐帧功能组项；seg_num/source_uid 为 _ABSENT 时不写对应序列。"""
    item = Dataset()
    if source_uid is not _ABSENT:
        src = Dataset()
        src.ReferencedSOPClassUID = CTImageStorage
        if source_uid is not None:
            src.ReferencedSOPInstanceUID = source_uid
        deriv = Dataset()
        deriv.SourceImageSequence = [src]
        item.DerivationImageSequence = [deriv]
    if seg_num is not _ABSENT:
        sid = Dataset()
        if seg_num is not None:
            sid.ReferencedSegmentNumber = seg_num
        item.SegmentIdentificationSequence = [sid]
    return item


def _fg_item_sources(seg_num, deriv_sources) -> Dataset:
    """逐帧功能组项，可写多个 DerivationImageSequence/SourceImageSequence。

    deriv_sources 为每个 DerivationImageSequence 项一个列表，列表元素是该
    SourceImageSequence 各项引用的源 UID（None 表示空 ReferencedSOPInstanceUID）；
    deriv_sources=_ABSENT 时不写 DerivationImageSequence。
    """
    item = Dataset()
    if deriv_sources is not _ABSENT:
        derivations = []
        for source_uids in deriv_sources:
            deriv = Dataset()
            srcs = []
            for uid in source_uids:
                src = Dataset()
                src.ReferencedSOPClassUID = CTImageStorage
                if uid is not None:
                    src.ReferencedSOPInstanceUID = uid
                srcs.append(src)
            deriv.SourceImageSequence = srcs
            derivations.append(deriv)
        item.DerivationImageSequence = derivations
    if seg_num is not _ABSENT:
        sid = Dataset()
        if seg_num is not None:
            sid.ReferencedSegmentNumber = seg_num
        item.SegmentIdentificationSequence = [sid]
    return item


def _pack_frames(frames_pixels: list[list[int]], rows: int, columns: int) -> bytes:
    """把每帧的前景像素下标列表打包为 DICOM 1-bit（LSB 优先）字节流。"""
    bits = []
    for pixels in frames_pixels:
        frame = np.zeros(rows * columns, dtype=np.uint8)
        for p in pixels:
            frame[p] = 1
        bits.append(frame)
    return np.packbits(np.concatenate(bits), bitorder="little").tobytes()


def _seg_bytes(
    sop_uid: str = SEG_SOP,
    frames: list[Dataset] = (),
    rows: int = 4,
    columns: int = 4,
    *,
    frames_pixels: list[list[int]] | None = None,
    seg_type: str | None = "BINARY",
    number_of_frames=_AUTO,
    transfer_syntax=ExplicitVRLittleEndian,
    bits_allocated: int = 1,
    with_pixel_data: bool = True,
    pixel_bytes: bytes | None = None,
    with_per_frame_sequence: bool = True,
) -> bytes:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SegmentationStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = transfer_syntax
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = SegmentationStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = STUDY
    ds.SeriesInstanceUID = SERIES_SEG
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.Modality = "SEG"
    if seg_type is not None:
        ds.SegmentationType = seg_type
    ds.Rows = rows
    ds.Columns = columns
    declared = len(frames) if number_of_frames is _AUTO else number_of_frames
    if declared is not None:
        ds.NumberOfFrames = declared
    ds.BitsAllocated = bits_allocated
    ds.BitsStored = 1
    ds.HighBit = 0
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    if with_per_frame_sequence:
        ds.PerFrameFunctionalGroupsSequence = list(frames)
    if with_pixel_data:
        if pixel_bytes is None:
            if frames_pixels is None:
                frames_pixels = [[] for _ in range(len(frames))]
            pixel_bytes = _pack_frames(frames_pixels, rows, columns)
        ds.PixelData = pixel_bytes
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
        files={"file": ("seg.zip", _zip_bytes(files), "application/zip")},
        data={"secret": SECRET, "shift_dates": "false"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _anon(original: str) -> str:
    return derive_uid(SECRET, original)


def _seg_coverage(client, job_id: str, sop_uid: str = SEG_SOP):
    return client.get(f"/jobs/{job_id}/instances/{_anon(sop_uid)}/seg-coverage")


def _output_path(client, job_id: str, sop_uid: str) -> str:
    from app.main import registry
    job_obj = registry.get(job_id)
    return os.path.join(job_obj.result.output_dir, f"{_anon(sop_uid)}.dcm")


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
def test_seg_coverage_basic_union_and_sorting(client):
    # CT1: seg1 两帧并集 {0,1}、seg2 一帧 {0,1,2}；CT2: seg1 一帧全前景
    files = {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "ct2.dcm": _ct_image_bytes(CT2_SOP),
        "seg.dcm": _seg_bytes(
            frames=[
                _fg_item(1, CT1_SOP),
                _fg_item(1, CT1_SOP),
                _fg_item(2, CT1_SOP),
                _fg_item(1, CT2_SOP),
            ],
            frames_pixels=[[0], [1], [0, 1, 2], list(range(16))],
        ),
    }
    job = _upload(client, files)
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["sop_instance_uid"] == _anon(SEG_SOP)
    assert data["output_file"] == f"{_anon(SEG_SOP)}.dcm"
    assert data["segmentation_type"] == "BINARY"
    assert data["rows"] == 4
    assert data["columns"] == 4
    assert data["number_of_frames"] == 4
    assert data["group_count"] == 3

    by_key = {
        (g["source_sop_instance_uid"], g["segment_number"]): g
        for g in data["groups"]
    }
    g1 = by_key[(_anon(CT1_SOP), 1)]
    assert g1["frame_count"] == 2
    assert g1["foreground_pixels"] == 2       # 并集 {0,1}，不是 1+1=2 的简单相加语义
    assert g1["image_pixels"] == 16
    assert g1["coverage"] == pytest.approx(2 / 16)
    assert g1["source_output_file"] == f"{_anon(CT1_SOP)}.dcm"

    g2 = by_key[(_anon(CT1_SOP), 2)]
    assert g2["frame_count"] == 1
    assert g2["foreground_pixels"] == 3
    assert g2["coverage"] == pytest.approx(3 / 16)

    g3 = by_key[(_anon(CT2_SOP), 1)]
    assert g3["foreground_pixels"] == 16
    assert g3["coverage"] == pytest.approx(1.0)

    # 按（源 UID, 段号）排序
    keys = [
        (g["source_sop_instance_uid"], g["segment_number"])
        for g in data["groups"]
    ]
    assert keys == sorted(keys)


def test_seg_coverage_union_not_sum(client):
    # 两帧完全重叠：并集前景为 2，而非 2+2=4
    files = {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP), _fg_item(1, CT1_SOP)],
            frames_pixels=[[0, 1], [0, 1]],
        ),
    }
    job = _upload(client, files)
    data = _seg_coverage(client, job["id"]).json()
    group = data["groups"][0]
    assert group["frame_count"] == 2
    assert group["foreground_pixels"] == 2
    assert group["coverage"] == pytest.approx(2 / 16)


def test_seg_coverage_single_frame(client):
    files = {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(3, CT1_SOP)],
            frames_pixels=[[5, 6, 7]],
        ),
    }
    job = _upload(client, files)
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["number_of_frames"] == 1
    group = data["groups"][0]
    assert group["segment_number"] == 3
    assert group["foreground_pixels"] == 3
    assert group["image_pixels"] == 16


def test_seg_coverage_non_byte_aligned_dimensions(client):
    # 3x3 = 9 bit/帧，跨字节边界；前景为最后一帧的最后一个像素
    files = {
        "ct1.dcm": _ct_image_bytes(CT1_SOP, rows=3, columns=3),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP), _fg_item(1, CT1_SOP)],
            rows=3,
            columns=3,
            frames_pixels=[[0], [8]],
        ),
    }
    job = _upload(client, files)
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 200, r.text
    group = r.json()["groups"][0]
    assert group["foreground_pixels"] == 2
    assert group["image_pixels"] == 9
    assert group["coverage"] == pytest.approx(2 / 9)


def test_seg_coverage_implicit_vr_little_endian(client):
    files = {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[0, 15]],
            transfer_syntax=ImplicitVRLittleEndian,
        ),
    }
    job = _upload(client, files)
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 200, r.text
    group = r.json()["groups"][0]
    assert group["foreground_pixels"] == 2


def test_seg_coverage_empty_mask_zero_coverage(client):
    files = {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[]],
        ),
    }
    job = _upload(client, files)
    data = _seg_coverage(client, job["id"]).json()
    group = data["groups"][0]
    assert group["foreground_pixels"] == 0
    assert group["coverage"] == 0.0


def test_seg_coverage_only_requested_instance_parsed(client):
    files = {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg1.dcm": _seg_bytes(
            SEG_SOP,
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[0]],
        ),
        "seg2.dcm": _seg_bytes(
            SEG2_SOP,
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[list(range(16))],
        ),
    }
    job = _upload(client, files)
    data = _seg_coverage(client, job["id"], SEG_SOP).json()
    assert data["sop_instance_uid"] == _anon(SEG_SOP)
    assert data["groups"][0]["foreground_pixels"] == 1


# --------------------------------------------------------------------------- #
# 404
# --------------------------------------------------------------------------- #
def test_seg_coverage_job_not_found_404(client):
    r = client.get(f"/jobs/deadbeef/instances/{_anon(SEG_SOP)}/seg-coverage")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "JOB_NOT_FOUND"
    assert detail["sop_instance_uid"] == _anon(SEG_SOP)


def test_seg_coverage_instance_not_found_404(client):
    job = _upload(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(frames=[_fg_item(1, CT1_SOP)], frames_pixels=[[0]]),
    })
    r = client.get(f"/jobs/{job['id']}/instances/{_anon('9.9.9')}/seg-coverage")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "INSTANCE_NOT_FOUND"
    assert detail["sop_instance_uid"] == _anon("9.9.9")


def test_seg_coverage_output_file_removed_404(client):
    job = _upload(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(frames=[_fg_item(1, CT1_SOP)], frames_pixels=[[0]]),
    })
    os.remove(_output_path(client, job["id"], SEG_SOP))
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "INSTANCE_OUTPUT_MISSING"
    assert detail["reason"] == "output_missing"
    assert detail["sop_instance_uid"] == _anon(SEG_SOP)


# --------------------------------------------------------------------------- #
# 409：任务/输出定位
# --------------------------------------------------------------------------- #
def test_seg_coverage_failed_job_409(client):
    r = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": SECRET},
    )
    job = r.json()
    assert job["status"] == "failed"
    resp = client.get(f"/jobs/{job['id']}/instances/x/seg-coverage")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "JOB_NOT_READY"
    assert detail["sop_instance_uid"] == "x"


def test_seg_coverage_ambiguous_output_409(client):
    # 输入两个同 SOP UID 的 SEG -> 输出 <uid>.dcm / <uid>-2.dcm
    files = {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "dup_a.dcm": _seg_bytes(frames=[_fg_item(1, CT1_SOP)], frames_pixels=[[0]]),
        "dup_b.dcm": _seg_bytes(frames=[_fg_item(1, CT1_SOP)], frames_pixels=[[0]]),
    }
    job = _upload(client, files)
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "AMBIGUOUS_INSTANCE_OUTPUT"
    assert detail["reason"] == "ambiguous_output"
    assert detail["sop_instance_uid"] == _anon(SEG_SOP)
    assert detail["output_files"] == sorted(
        [f"{_anon(SEG_SOP)}.dcm", f"{_anon(SEG_SOP)}-2.dcm"]
    )


# --------------------------------------------------------------------------- #
# 409：文件级（非 SEG / 非 BINARY / 压缩 / UID 不符 / 不可读）
# --------------------------------------------------------------------------- #
def _conflict(client, files, seg_uid: str = SEG_SOP):
    job = _upload(client, files)
    r = _seg_coverage(client, job["id"], seg_uid)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "SEG_COVERAGE_CONFLICT"
    assert detail["sop_instance_uid"] == _anon(seg_uid)
    # 不输出部分统计
    assert "groups" not in detail
    assert detail["conflict_count"] == len(detail["problems"])
    return detail


def test_seg_coverage_not_segmentation_409(client):
    detail = _conflict(client, {"ct.dcm": _ct_image_bytes(SEG_SOP)})
    problems = [p for p in detail["problems"] if p["reason"] == "not_segmentation"]
    assert len(problems) == 1
    assert problems[0]["sop_instance_uid"] == _anon(SEG_SOP)


def test_seg_coverage_fractional_type_409(client):
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[0]],
            seg_type="FRACTIONAL",
        ),
    })
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "unsupported_segmentation_type"
    ]
    assert len(problems) == 1
    assert problems[0]["segmentation_type"] == "FRACTIONAL"


def test_seg_coverage_missing_segmentation_type_409(client):
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[0]],
            seg_type=None,
        ),
    })
    assert any(
        p["reason"] == "unsupported_segmentation_type" for p in detail["problems"]
    )


def test_seg_coverage_compressed_transfer_syntax_409(client):
    # 封装（压缩）传输语法：像素数据须为 encapsulated 才能写出；
    # 接口在解码前就应按传输语法拒绝
    from pydicom.encaps import encapsulate
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            transfer_syntax=JPEGBaseline8Bit,
            pixel_bytes=encapsulate([b"\x01\x00"]),
        ),
    })
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "unsupported_transfer_syntax"
    ]
    assert len(problems) == 1
    assert problems[0]["transfer_syntax_uid"] == str(JPEGBaseline8Bit)


def test_seg_coverage_unreadable_output_409(client):
    job = _upload(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(frames=[_fg_item(1, CT1_SOP)], frames_pixels=[[0]]),
    })
    with open(_output_path(client, job["id"], SEG_SOP), "wb") as fh:
        fh.write(b"\x00\x01not dicom anymore")
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SEG_COVERAGE_CONFLICT"
    problems = [p for p in detail["problems"] if p["reason"] == "unreadable"]
    assert len(problems) == 1
    assert problems[0]["sop_instance_uid"] == _anon(SEG_SOP)


def test_seg_coverage_sop_uid_mismatch_409(client):
    job = _upload(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(frames=[_fg_item(1, CT1_SOP)], frames_pixels=[[0]]),
    })
    # 篡改磁盘上的匿名输出：文件内 SOPInstanceUID 与请求不符
    import pydicom
    path = _output_path(client, job["id"], SEG_SOP)
    ds = pydicom.dcmread(path)
    ds.SOPInstanceUID = "2.25.999999999"
    ds.save_as(path, enforce_file_format=True)
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SEG_COVERAGE_CONFLICT"
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "sop_instance_uid_mismatch"
    ]
    assert len(problems) == 1


# --------------------------------------------------------------------------- #
# 409：帧数与像素数据异常
# --------------------------------------------------------------------------- #
def test_seg_coverage_non_binary_bit_depth_409(client):
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            bits_allocated=8,
            pixel_bytes=bytes(16),
        ),
    })
    problems = [
        p for p in detail["problems"] if p["reason"] == "invalid_pixel_metadata"
    ]
    assert len(problems) == 1


def test_seg_coverage_missing_number_of_frames_409(client):
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[0]],
            number_of_frames=None,
        ),
    })
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "invalid_number_of_frames"
    ]
    assert len(problems) == 1
    assert problems[0]["declared_number_of_frames"] == ""


def test_seg_coverage_invalid_number_of_frames_409(client):
    # IS 取值合法但非正数，无法确定帧数
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[0]],
            number_of_frames="0",
        ),
    })
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "invalid_number_of_frames"
    ]
    assert len(problems) == 1
    assert problems[0]["declared_number_of_frames"] == "0"


def test_seg_coverage_frame_count_mismatch_409(client):
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],  # 逐帧功能组只有 1 项
            frames_pixels=[[0], [1]],
            number_of_frames=2,             # 但声明 2 帧
        ),
    })
    problems = [
        p for p in detail["problems"] if p["reason"] == "frame_count_mismatch"
    ]
    assert len(problems) == 1
    assert problems[0]["declared_number_of_frames"] == "2"
    assert problems[0]["per_frame_item_count"] == 1


def test_seg_coverage_missing_per_frame_sequence_409(client):
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[0]],
            with_per_frame_sequence=False,
        ),
    })
    problems = [
        p for p in detail["problems"] if p["reason"] == "frame_count_mismatch"
    ]
    assert len(problems) == 1
    assert problems[0]["per_frame_item_count"] == 0


def test_seg_coverage_missing_pixel_data_409(client):
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            with_pixel_data=False,
        ),
    })
    problems = [
        p for p in detail["problems"] if p["reason"] == "missing_pixel_data"
    ]
    assert len(problems) == 1


def test_seg_coverage_truncated_pixel_data_409(client):
    # 2 帧 4x4 需要 4 字节，只给 2 字节
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP), _fg_item(1, CT1_SOP)],
            pixel_bytes=b"\x01\x00",
        ),
    })
    problems = [
        p for p in detail["problems"] if p["reason"] == "pixel_decode_failed"
    ]
    assert len(problems) == 1


# --------------------------------------------------------------------------- #
# 409：帧引用缺失 / 源图像问题
# --------------------------------------------------------------------------- #
def test_seg_coverage_missing_segment_number_409(client):
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP), _fg_item(_ABSENT, CT1_SOP)],
            frames_pixels=[[0], [1]],
        ),
    })
    problems = [
        p for p in detail["problems"] if p["reason"] == "missing_segment_number"
    ]
    assert len(problems) == 1
    assert problems[0]["frame_index"] == 1


def test_seg_coverage_missing_source_reference_409(client):
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, _ABSENT)],
            frames_pixels=[[0]],
        ),
    })
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "missing_source_reference"
    ]
    assert len(problems) == 1
    assert problems[0]["frame_index"] == 0


def test_seg_coverage_source_not_in_output_409(client):
    # 引用一个从未上传的 SOP UID（匿名化后成为悬空引用）
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, "9.9.9")],
            frames_pixels=[[0]],
        ),
    })
    problems = [
        p for p in detail["problems"] if p["reason"] == "source_not_in_output"
    ]
    assert len(problems) == 1
    assert problems[0]["source_sop_instance_uid"] == _anon("9.9.9")


def test_seg_coverage_source_ambiguous_output_409(client):
    # 源图像 SOP UID 在任务内有两份输出 -> 引用歧义
    detail = _conflict(client, {
        "ct_a.dcm": _ct_image_bytes(CT1_SOP),
        "ct_b.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[0]],
        ),
    })
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "source_ambiguous_output"
    ]
    assert len(problems) == 1
    assert problems[0]["source_sop_instance_uid"] == _anon(CT1_SOP)
    assert problems[0]["output_files"] == sorted(
        [f"{_anon(CT1_SOP)}.dcm", f"{_anon(CT1_SOP)}-2.dcm"]
    )


def test_seg_coverage_source_dimension_mismatch_409(client):
    # 源图像 2x8，SEG 掩码 4x4
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP, rows=2, columns=8),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[0]],
        ),
    })
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "source_dimension_mismatch"
    ]
    assert len(problems) == 1
    p = problems[0]
    assert p["source_sop_instance_uid"] == _anon(CT1_SOP)
    assert p["source_rows"] == 2
    assert p["source_columns"] == 8
    assert p["source_output_file"] == f"{_anon(CT1_SOP)}.dcm"


def test_seg_coverage_source_output_missing_409(client):
    job = _upload(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(frames=[_fg_item(1, CT1_SOP)], frames_pixels=[[0]]),
    })
    os.remove(_output_path(client, job["id"], CT1_SOP))
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SEG_COVERAGE_CONFLICT"
    problems = [
        p for p in detail["problems"] if p["reason"] == "source_output_missing"
    ]
    assert len(problems) == 1
    assert problems[0]["source_sop_instance_uid"] == _anon(CT1_SOP)


def test_seg_coverage_source_unreadable_409(client):
    job = _upload(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(frames=[_fg_item(1, CT1_SOP)], frames_pixels=[[0]]),
    })
    with open(_output_path(client, job["id"], CT1_SOP), "wb") as fh:
        fh.write(b"\x00\x01not dicom anymore")
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SEG_COVERAGE_CONFLICT"
    problems = [
        p for p in detail["problems"] if p["reason"] == "source_unreadable"
    ]
    assert len(problems) == 1
    assert problems[0]["source_sop_instance_uid"] == _anon(CT1_SOP)


def test_seg_coverage_collects_multiple_problems_at_once(client):
    # 一帧缺段号、另一帧引用不存在的源图像：同一次 409 全部给出
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(_ABSENT, CT1_SOP), _fg_item(1, "9.9.9")],
            frames_pixels=[[0], [1]],
        ),
    })
    reasons = {p["reason"] for p in detail["problems"]}
    assert "missing_segment_number" in reasons
    assert "source_not_in_output" in reasons


def test_seg_coverage_no_partial_stats(client):
    # 一帧合法、一帧引用缺失：整体 409，不输出部分分组统计
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP), _fg_item(1, "9.9.9")],
            frames_pixels=[[0], [1]],
        ),
    })
    assert detail["problems"]
    assert all(p["sop_instance_uid"] == _anon(SEG_SOP) for p in detail["problems"])


def test_seg_coverage_response_contains_only_anonymized_uids(client):
    """成功与冲突响应都不得泄露原始 SOP UID。"""
    files = {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, CT1_SOP)],
            frames_pixels=[[0]],
        ),
    }
    job = _upload(client, files)
    ok = _seg_coverage(client, job["id"]).json()
    assert CT1_SOP not in str(ok)
    assert SEG_SOP not in str(ok)
    assert _anon(CT1_SOP) in str(ok)

    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item(1, "9.9.9")],
            frames_pixels=[[0]],
        ),
    })
    raw = str(detail)
    assert "9.9.9" not in raw
    assert _anon("9.9.9") in raw


# --------------------------------------------------------------------------- #
# 409：同一帧引用多个不同源 SOP UID（多源分割不得静默归给首个引用）
# --------------------------------------------------------------------------- #
def test_seg_coverage_two_derivations_two_sources_409(client):
    # 同一帧在两个 DerivationImageSequence 项中分别引用 CT1 与 CT2：
    # 旧逻辑只读 [0]/[0] 会把该帧静默算作 CT1 的覆盖率
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "ct2.dcm": _ct_image_bytes(CT2_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item_sources(1, [[CT1_SOP], [CT2_SOP]])],
            frames_pixels=[[0]],
        ),
    })
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "multiple_source_references"
    ]
    assert len(problems) == 1
    p = problems[0]
    assert p["frame_index"] == 0
    assert p["source_sop_instance_uids"] == sorted(
        [_anon(CT1_SOP), _anon(CT2_SOP)]
    )
    # 不回显原始 UID
    assert CT1_SOP not in str(detail)
    assert CT2_SOP not in str(detail)
    # 不输出部分统计
    assert "groups" not in detail


def test_seg_coverage_two_sources_in_one_source_sequence_409(client):
    # 单个 DerivationImageSequence 的 SourceImageSequence 内两个不同源
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "ct2.dcm": _ct_image_bytes(CT2_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item_sources(1, [[CT1_SOP, CT2_SOP]])],
            frames_pixels=[[0]],
        ),
    })
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "multiple_source_references"
    ]
    assert len(problems) == 1
    assert problems[0]["frame_index"] == 0
    assert problems[0]["source_sop_instance_uids"] == sorted(
        [_anon(CT1_SOP), _anon(CT2_SOP)]
    )


def test_seg_coverage_multiple_conflicting_frames_listed_together(client):
    # 第 0 帧与第 2 帧各引用两个不同源 UID，第 1 帧正常：
    # 同一次 409 必须汇总全部冲突帧号与冲突 UID，不输出部分统计
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "ct2.dcm": _ct_image_bytes(CT2_SOP),
        "seg.dcm": _seg_bytes(
            frames=[
                _fg_item_sources(1, [[CT1_SOP], [CT2_SOP]]),
                _fg_item(1, CT1_SOP),
                _fg_item_sources(2, [[CT2_SOP, CT1_SOP]]),
            ],
            frames_pixels=[[0], [1], [2]],
        ),
    })
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "multiple_source_references"
    ]
    assert [p["frame_index"] for p in problems] == [0, 2]
    for p in problems:
        assert p["source_sop_instance_uids"] == sorted(
            [_anon(CT1_SOP), _anon(CT2_SOP)]
        )
    assert detail["conflict_count"] == len(detail["problems"]) == 2
    assert "groups" not in detail


def test_seg_coverage_duplicate_same_uid_across_sequences_is_single_source(client):
    # 同一 UID 在不同 DerivationImageSequence/SourceImageSequence 中重复出现：
    # 去重后恰有一个唯一 UID，结果与单一引用完全一致（顺序与数值不变）
    files = {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "seg.dcm": _seg_bytes(
            frames=[
                _fg_item_sources(1, [[CT1_SOP], [CT1_SOP, CT1_SOP]]),
                _fg_item(1, CT1_SOP),
            ],
            frames_pixels=[[0], [1]],
        ),
    }
    job = _upload(client, files)
    r = _seg_coverage(client, job["id"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["group_count"] == 1
    group = data["groups"][0]
    assert group["source_sop_instance_uid"] == _anon(CT1_SOP)
    assert group["segment_number"] == 1
    assert group["frame_count"] == 2
    assert group["foreground_pixels"] == 2
    assert group["coverage"] == pytest.approx(2 / 16)


def test_seg_coverage_multi_source_conflict_checks_before_source_lookup(client):
    # 冲突 UID 即使不在任务输出中，也先按同帧多源冲突报告（不静默归给首个引用）
    detail = _conflict(client, {
        "ct1.dcm": _ct_image_bytes(CT1_SOP),
        "ct2.dcm": _ct_image_bytes(CT2_SOP),
        "seg.dcm": _seg_bytes(
            frames=[_fg_item_sources(1, [[CT1_SOP], ["9.9.9"]])],
            frames_pixels=[[0]],
        ),
    })
    reasons = {p["reason"] for p in detail["problems"]}
    assert "multiple_source_references" in reasons
    problems = [
        p for p in detail["problems"]
        if p["reason"] == "multiple_source_references"
    ]
    assert problems[0]["source_sop_instance_uids"] == sorted(
        [_anon(CT1_SOP), _anon("9.9.9")]
    )


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
def test_seg_coverage_endpoint_in_openapi(client):
    r = client.get("/openapi.json")
    paths = r.json()["paths"]
    key = "/jobs/{job_id}/instances/{sop_instance_uid}/seg-coverage"
    assert key in paths
    assert "get" in paths[key]
    schema = paths[key]["get"]
    assert "SegCoverageOut" in str(schema["responses"]["200"])
    # 根路径端点清单也包含新接口
    endpoints = client.get("/").json()["endpoints"]
    assert "GET /jobs/{job_id}/instances/{sop_instance_uid}/seg-coverage" in endpoints
