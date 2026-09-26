"""按匿名 SOPInstanceUID 渲染 PNG 预览：定位、灰度管线、不支持类型、404/409/422。"""
from __future__ import annotations

import io
import zipfile

import numpy as np
import pydicom
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    ExplicitVRBigEndian,
    ImplicitVRLittleEndian,
    RTStructureSetStorage,
    UID,
)

from app.crypto import derive_uid
from tests.sample_data import _write_bytes

SECRET = "preview-secret-123456"


def _write_ds_bytes(ds: FileDataset, transfer_syntax) -> bytes:
    """按传输语法写出：大端需手工转换像素字节序，封装语法需预先 encapsulate。"""
    if transfer_syntax == ExplicitVRBigEndian:
        if "PixelData" in ds:
            rows = int(ds.Rows)
            cols = int(ds.Columns)
            n = rows * cols
            width = "u1" if int(ds.BitsAllocated) == 8 else "u2"
            raw = np.frombuffer(ds.PixelData, dtype=f"<{width}", count=n)
            ds.PixelData = raw.astype(f">{width}").tobytes()
        # pydicom 3.x：little_endian=False 会把非像素元素转为大端写出
        buf = io.BytesIO()
        pydicom.dcmwrite(buf, ds, enforce_file_format=True, little_endian=False)
        return buf.getvalue()
    if str(transfer_syntax).startswith("1.2.840.10008.1.2.4") or \
            str(transfer_syntax) == "1.2.840.10008.1.2.5":
        # 封装像素数据：填入伪 JPEG/RLE 帧（不要求可解码，渲染器在传输语法层即拒绝）
        ds.PixelData = encapsulate([ds.PixelData])
    return _write_bytes(ds)


# --------------------------------------------------------------------------- #
# 合成 DICOM
# --------------------------------------------------------------------------- #
def _image_bytes(
    sop_uid: str,
    *,
    pixels: np.ndarray,
    photometric: str = "MONOCHROME2",
    bits_allocated: int = 16,
    bits_stored: int | None = None,
    high_bit: int | None = None,
    pixel_representation: int = 0,
    transfer_syntax=ExplicitVRLittleEndian,
    study_uid: str = "3.1",
    series_uid: str = "2.1",
    rescale_slope=None,
    rescale_intercept=None,
    window_center=None,
    window_width=None,
    number_of_frames=None,
    samples_per_pixel: int = 1,
    sop_class=CTImageStorage,
    include_pixel_data: bool = True,
) -> bytes:
    bits_stored = bits_allocated if bits_stored is None else bits_stored
    high_bit = bits_stored - 1 if high_bit is None else high_bit

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = sop_class
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = transfer_syntax
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.Modality = "CT"
    ds.SamplesPerPixel = samples_per_pixel
    ds.PhotometricInterpretation = photometric
    ds.Rows, ds.Columns = pixels.shape
    ds.BitsAllocated = bits_allocated
    ds.BitsStored = bits_stored
    ds.HighBit = high_bit
    ds.PixelRepresentation = pixel_representation
    if number_of_frames is not None:
        ds.NumberOfFrames = number_of_frames
    if rescale_slope is not None:
        ds.RescaleSlope = rescale_slope
    if rescale_intercept is not None:
        ds.RescaleIntercept = rescale_intercept
    if window_center is not None:
        ds.WindowCenter = window_center
    if window_width is not None:
        ds.WindowWidth = window_width
    if include_pixel_data:
        ds.PixelData = pixels.tobytes()
    return _write_ds_bytes(ds, transfer_syntax)


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


def _preview(client, job_id: str, sop_uid: str):
    return client.get(f"/jobs/{job_id}/instances/{sop_uid}/preview.png")


def _png_array(content: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(content)))


def _anon(original: str) -> str:
    return derive_uid(SECRET, original)


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
def test_preview_png_basic_16bit_minmax(client):
    arr = np.array([[0, 1000], [2000, 4000]], dtype=np.uint16)
    files = {"a.dcm": _image_bytes("1.1.1", pixels=arr)}
    job = _upload(client, files)

    r = _preview(client, job["id"], _anon("1.1.1"))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    img = _png_array(r.content)
    assert img.shape == (2, 2)
    assert img.dtype == np.uint8
    # 缺窗 -> 极值拉伸：0 -> 0，4000 -> 255
    assert tuple(img[0]) == (0, 64)
    assert tuple(img[1]) == (128, 255)
    assert r.headers["x-preview-window"] == "minmax"
    assert r.headers["x-preview-photometric-interpretation"] == "MONOCHROME2"
    assert r.headers["x-preview-bits-allocated"] == "16"
    assert r.headers["x-preview-rescale-slope"] == "1.0"
    assert r.headers["x-preview-rescale-intercept"] == "0.0"
    assert r.headers["content-disposition"].startswith('inline; filename="preview-')
    assert r.headers["content-disposition"].endswith('.png"')


def test_preview_png_8bit(client):
    arr = np.array([[0, 64], [128, 255]], dtype=np.uint8)
    files = {"a.dcm": _image_bytes("1.1.8", pixels=arr, bits_allocated=8)}
    job = _upload(client, files)
    r = _preview(client, job["id"], _anon("1.1.8"))
    assert r.status_code == 200, r.text
    img = _png_array(r.content)
    assert tuple(img[0]) == (0, 64)
    assert tuple(img[1]) == (128, 255)
    assert r.headers["x-preview-bits-allocated"] == "8"
    assert r.headers["x-preview-bits-stored"] == "8"


def test_preview_monochrome1_inverted_last(client):
    arr = np.array([[0, 1000], [2000, 4000]], dtype=np.uint16)
    files = {"a.dcm": _image_bytes("1.1.11", pixels=arr, photometric="MONOCHROME1")}
    job = _upload(client, files)
    r = _preview(client, job["id"], _anon("1.1.11"))
    assert r.status_code == 200, r.text
    img = _png_array(r.content)
    # MONOCHROME2 缺窗拉伸结果为 (0,64,128,255)；MONOCHROME1 最后反相
    assert tuple(img[0]) == (255, 191)
    assert tuple(img[1]) == (127, 0)


def test_preview_voi_window_first_value(client):
    # 4 个梯度像素，窗宽窗位正好使输出近似线性覆盖 0..255
    arr = np.array([[0, 1000], [2000, 4000]], dtype=np.uint16)
    files = {
        "a.dcm": _image_bytes(
            "1.1.12", pixels=arr, window_center="2000\\9999", window_width="4001\\1"
        )
    }
    job = _upload(client, files)
    r = _preview(client, job["id"], _anon("1.1.12"))
    assert r.status_code == 200, r.text
    img = _png_array(r.content)
    # c=2000, w=4001（w-1=4000）：y = x/4000*255
    assert tuple(img[0]) == (0, 64)
    assert tuple(img[1]) == (128, 255)
    assert r.headers["x-preview-window"] == "voi"


def test_preview_voi_window_clamps_outside_range(client):
    # c=100,w=200 -> 窗内区间 0.5..199.5：0 与 200 均在窗外，分别钳为 0/255；
    # 窗内 100（窗位）-> 128、50 -> 64
    arr = np.array([[0, 50], [100, 200]], dtype=np.uint16)
    files = {
        "a.dcm": _image_bytes(
            "1.1.100", pixels=arr, window_center=100, window_width=200
        )
    }
    job = _upload(client, files)
    img = _png_array(_preview(client, job["id"], _anon("1.1.100")).content)
    assert tuple(img[0]) == (0, 64)
    assert tuple(img[1]) == (128, 255)


def test_preview_rescale_applied_before_window(client):
    # slope=2, intercept=10：[0,100] -> [10,210]；缺窗极值拉伸
    arr = np.array([[0, 25], [75, 100]], dtype=np.uint16)
    files = {
        "a.dcm": _image_bytes(
            "1.1.13", pixels=arr, rescale_slope=2, rescale_intercept=10
        )
    }
    job = _upload(client, files)
    r = _preview(client, job["id"], _anon("1.1.13"))
    assert r.status_code == 200, r.text
    img = _png_array(r.content)
    assert tuple(img[0]) == (0, 64)
    assert tuple(img[1]) == (191, 255)
    assert r.headers["x-preview-rescale-slope"] == "2.0"
    assert r.headers["x-preview-rescale-intercept"] == "10.0"


def test_preview_rescale_then_voi(client):
    # slope=1, intercept=-100，像素 0..400 -> -100..300；
    # 窗 c=100,w=401 使 -100->0、300->255
    arr = np.array([[0, 100], [200, 400]], dtype=np.uint16)
    files = {
        "a.dcm": _image_bytes(
            "1.1.14", pixels=arr, rescale_intercept=-100,
            window_center=100, window_width=401,
        )
    }
    job = _upload(client, files)
    img = _png_array(_preview(client, job["id"], _anon("1.1.14")).content)
    assert tuple(img[0]) == (0, 64)
    assert tuple(img[1]) == (128, 255)


def test_preview_signed_pixels(client):
    # 16 位有符号：-2000..2000 拉伸
    arr = np.array([[-2000, -1000], [1000, 2000]], dtype=np.int16).view(np.uint16)
    files = {"a.dcm": _image_bytes("1.1.15", pixels=arr, pixel_representation=1)}
    job = _upload(client, files)
    r = _preview(client, job["id"], _anon("1.1.15"))
    assert r.status_code == 200, r.text
    img = _png_array(r.content)
    assert tuple(img[0]) == (0, 64)
    assert tuple(img[1]) == (191, 255)


def test_preview_bits_stored_masking(client):
    # 12 位存储于 16 位：高位垃圾位必须被 pydicom 按 BitsStored 掩码忽略
    arr = np.array([[0x0000, 0x0FFF], [0x0800, 0xF001]], dtype=np.uint16)
    files = {
        "a.dcm": _image_bytes(
            "1.1.16", pixels=arr, bits_allocated=16, bits_stored=12, high_bit=11
        )
    }
    job = _upload(client, files)
    img = _png_array(_preview(client, job["id"], _anon("1.1.16")).content)
    # 解码后为 [0,4095,2048,1] -> 拉伸
    assert img[0, 0] == 0
    assert img[0, 1] == 255
    assert img[1, 0] == 128
    assert img[1, 1] == 0


def test_preview_implicit_and_big_endian_transfer_syntaxes(client):
    arr = np.array([[0, 100], [200, 400]], dtype=np.uint16)
    files = {
        "le.dcm": _image_bytes("1.1.171", pixels=arr, transfer_syntax=ImplicitVRLittleEndian),
        "be.dcm": _image_bytes("1.1.172", pixels=arr, transfer_syntax=ExplicitVRBigEndian),
    }
    job = _upload(client, files)
    for uid in ("1.1.171", "1.1.172"):
        r = _preview(client, job["id"], _anon(uid))
        assert r.status_code == 200, (uid, r.text)
        img = _png_array(r.content)
        assert tuple(img[0]) == (0, 64)
        assert tuple(img[1]) == (128, 255)


def test_preview_constant_image_minmax(client):
    arr = np.full((2, 2), 1234, dtype=np.uint16)
    files = {"a.dcm": _image_bytes("1.1.19", pixels=arr)}
    job = _upload(client, files)
    img = _png_array(_preview(client, job["id"], _anon("1.1.19")).content)
    # 常量影像缺窗拉伸：MONOCHROME2 全黑
    assert set(img.ravel()) == {0}


def test_preview_one_value_window_width(client):
    # 退化窗宽 w=1：按窗位二值化
    arr = np.array([[99, 100], [101, 200]], dtype=np.uint16)
    files = {
        "a.dcm": _image_bytes(
            "1.1.18", pixels=arr, window_center=100, window_width=1
        )
    }
    job = _upload(client, files)
    img = _png_array(_preview(client, job["id"], _anon("1.1.18")).content)
    assert tuple(img[0]) == (0, 255)
    assert tuple(img[1]) == (255, 255)


# --------------------------------------------------------------------------- #
# 404 / 409
# --------------------------------------------------------------------------- #
def test_preview_job_not_found_404(client):
    r = _preview(client, "deadbeef", "2.25.whatever")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "JOB_NOT_FOUND"
    assert detail["sop_instance_uid"] == "2.25.whatever"


def test_preview_instance_not_found_404(client):
    files = {"a.dcm": _image_bytes(
        "1.1.1", pixels=np.zeros((2, 2), dtype=np.uint16))}
    job = _upload(client, files)
    r = _preview(client, job["id"], "2.25.unknown-anon-sop")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "INSTANCE_NOT_FOUND"
    assert detail["sop_instance_uid"] == "2.25.unknown-anon-sop"


def test_preview_failed_job_409(client):
    r = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": SECRET},
    )
    job = r.json()
    assert job["status"] == "failed"
    resp = _preview(client, job["id"], "2.25.whatever")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "JOB_NOT_READY"


def test_preview_ambiguous_sop_409(client):
    dup = "1.5.5.777"
    arr = np.zeros((2, 2), dtype=np.uint16)
    files = {
        "dup_a.dcm": _image_bytes(dup, pixels=arr),
        "dup_b.dcm": _image_bytes(dup, pixels=arr),
    }
    job = _upload(client, files)
    r = _preview(client, job["id"], _anon(dup))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "AMBIGUOUS_INSTANCE_OUTPUT"
    assert detail["reason"] == "ambiguous_output"
    anon = _anon(dup)
    assert detail["output_files"] == sorted([f"{anon}.dcm", f"{anon}-2.dcm"])


def test_preview_output_file_removed_404(client):
    files = {"a.dcm": _image_bytes(
        "1.1.1", pixels=np.zeros((2, 2), dtype=np.uint16))}
    job = _upload(client, files)
    from app.main import registry
    job_obj = registry.get(job["id"])
    os_remove = __import__("os").remove
    for name in __import__("os").listdir(job_obj.result.output_dir):
        os_remove(__import__("os").path.join(job_obj.result.output_dir, name))
    r = _preview(client, job["id"], _anon("1.1.1"))
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "INSTANCE_OUTPUT_MISSING"
    assert detail["reason"] == "output_missing"
    assert detail["output_file"] == f"{_anon('1.1.1')}.dcm"


# --------------------------------------------------------------------------- #
# 422：无法解码 / 不支持的影像
# --------------------------------------------------------------------------- #
def _expect_unsupported(client, job, sop_uid: str, reason: str):
    r = _preview(client, job["id"], _anon(sop_uid))
    assert r.status_code == 422, (sop_uid, r.text)
    detail = r.json()["detail"]
    assert detail["code"] == "PREVIEW_UNSUPPORTED_IMAGE"
    assert detail["reason"] == reason
    assert detail["sop_instance_uid"] == _anon(sop_uid)
    assert detail["message"]
    return detail


def test_preview_compressed_transfer_syntax_422(client):
    arr = np.zeros((2, 2), dtype=np.uint16)
    # JPEG Baseline 封装像素数据：手工构造一段无法 native 解码的封装内容，
    # 渲染器应先按传输语法拒绝，而非去尝试解码
    files = {
        "jpeg.dcm": _image_bytes(
            "1.2.11",
            pixels=arr,
            transfer_syntax=UID("1.2.840.10008.1.2.4.50"),
        )
    }
    job = _upload(client, files)
    detail = _expect_unsupported(client, job, "1.2.11",
                                 "unsupported_transfer_syntax")
    assert detail["transfer_syntax_uid"] == "1.2.840.10008.1.2.4.50"


def test_preview_deflated_transfer_syntax_422(client):
    from pydicom.uid import DeflatedExplicitVRLittleEndian
    arr = np.zeros((2, 2), dtype=np.uint16)
    files = {
        "df.dcm": _image_bytes(
            "1.2.12", pixels=arr, transfer_syntax=DeflatedExplicitVRLittleEndian
        )
    }
    job = _upload(client, files)
    _expect_unsupported(client, job, "1.2.12", "unsupported_transfer_syntax")


def test_preview_multiframe_422(client):
    arr = np.zeros((2, 4), dtype=np.uint16)  # 2 帧 2x2
    files = {
        "mf.dcm": _image_bytes(
            "1.2.13", pixels=arr, number_of_frames=2
        )
    }
    job = _upload(client, files)
    _expect_unsupported(client, job, "1.2.13", "multiframe")


def test_preview_rgb_422(client):
    arr = np.zeros((2, 6), dtype=np.uint8)
    files = {
        "rgb.dcm": _image_bytes(
            "1.2.14",
            pixels=arr,
            photometric="RGB",
            bits_allocated=8,
            samples_per_pixel=3,
        )
    }
    job = _upload(client, files)
    _expect_unsupported(client, job, "1.2.14", "unsupported_samples_per_pixel")


def test_preview_palette_color_422(client):
    arr = np.zeros((2, 2), dtype=np.uint8)
    files = {
        "pal.dcm": _image_bytes(
            "1.2.15", pixels=arr, photometric="PALETTE COLOR", bits_allocated=8
        )
    }
    job = _upload(client, files)
    _expect_unsupported(client, job, "1.2.15",
                        "unsupported_photometric_interpretation")


def test_preview_32bit_422(client):
    arr = np.zeros((2, 4), dtype=np.uint16)  # 32 位/像素
    files = {
        "b32.dcm": _image_bytes(
            "1.2.16", pixels=arr, bits_allocated=32, bits_stored=32, high_bit=31
        )
    }
    job = _upload(client, files)
    _expect_unsupported(client, job, "1.2.16", "unsupported_bit_depth")


def test_preview_no_pixel_data_422(client):
    # RTSTRUCT：成功匿名输出，但没有像素
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = RTStructureSetStorage
    file_meta.MediaStorageSOPInstanceUID = "1.2.17"
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = RTStructureSetStorage
    ds.SOPInstanceUID = "1.2.17"
    ds.StudyInstanceUID = "3.1"
    ds.SeriesInstanceUID = "2.1"
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.Modality = "RTSTRUCT"
    files = {"rt.dcm": _write_bytes(ds)}
    job = _upload(client, files)
    _expect_unsupported(client, job, "1.2.17", "missing_pixel_data")


def test_preview_truncated_pixel_data_422(client):
    # Rows=Columns=4（每像素 2 字节需 32 字节），PixelData 只有一半字节
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = "1.2.18"
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = "1.2.18"
    ds.StudyInstanceUID = ds.SeriesInstanceUID = "3.1"
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = np.arange(8, dtype=np.uint16).tobytes()  # 16 字节，仅一半
    files = {"trunc.dcm": _write_bytes(ds)}
    job = _upload(client, files)
    _expect_unsupported(client, job, "1.2.18", "pixel_decode_failed")


def test_preview_invalid_rescale_422(client):
    # pydicom 3.x 会拒绝构造非法 DS 元素，改用 RawDataElement 注入坏字节
    from pydicom.dataelem import RawDataElement
    from pydicom.tag import Tag

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = "1.2.19"
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = "1.2.19"
    ds.StudyInstanceUID = ds.SeriesInstanceUID = "3.1"
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = ds.Columns = 2
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = np.zeros((2, 2), dtype=np.uint16).tobytes()
    ds[Tag(0x00281053)] = RawDataElement(
        Tag(0x00281053), "DS", 6, b"abc   ", 0, True, None
    )
    files = {"a.dcm": _write_bytes(ds)}
    job = _upload(client, files)
    _expect_unsupported(client, job, "1.2.19", "invalid_rescale")


def test_preview_invalid_window_422(client):
    arr = np.zeros((2, 2), dtype=np.uint16)
    files = {
        "zero_w.dcm": _image_bytes(
            "1.2.20", pixels=arr, window_center=100, window_width=0
        )
    }
    job = _upload(client, files)
    _expect_unsupported(client, job, "1.2.20", "invalid_voi_window")


def test_preview_only_window_center_present_uses_minmax(client):
    # 只有 WindowCenter 没有 WindowWidth：视为缺窗，极值拉伸
    arr = np.array([[0, 100], [200, 400]], dtype=np.uint16)
    files = {
        "a.dcm": _image_bytes("1.2.21", pixels=arr, window_center=100)
    }
    job = _upload(client, files)
    r = _preview(client, job["id"], _anon("1.2.21"))
    assert r.status_code == 200, r.text
    assert r.headers["x-preview-window"] == "minmax"
    img = _png_array(r.content)
    assert tuple(img[0]) == (0, 64)
    assert tuple(img[1]) == (128, 255)


# --------------------------------------------------------------------------- #
# 集成：标准样例 ZIP
# --------------------------------------------------------------------------- #
def test_preview_with_canonical_sample_zip(client, tmp_path):
    from tests.sample_data import build_sample_zip

    zip_path = tmp_path / "sample.zip"
    meta = build_sample_zip(str(zip_path), seed=1)
    with open(zip_path, "rb") as fh:
        r = client.post(
            "/jobs",
            files={"file": ("study.zip", fh, "application/zip")},
            data={"secret": SECRET, "shift_dates": "false"},
        )
    assert r.status_code == 201, r.text
    job = r.json()

    # 样例 CT 为 Explicit VR LE、单帧、16 位 MONOCHROME2、缺窗（无 Rescale/Window）
    r = _preview(client, job["id"], _anon(meta["sops"][0]))
    assert r.status_code == 200, r.text
    img = _png_array(r.content)
    assert img.shape == (4, 4)
    # bytes(range(32)) 按小端 uint16 解码（首像素 0x0100=256），缺窗极值拉伸
    decoded = np.frombuffer(bytes(range(32)), dtype="<u2").reshape(4, 4)
    stretched = np.rint(
        (decoded.astype(np.float64) - decoded.min())
        / (decoded.max() - decoded.min())
        * 255.0
    ).astype(np.uint8)
    assert np.array_equal(img, stretched)
    # 等间距 16 个梯度：0,17,34,…,255
    assert tuple(img.ravel()) == tuple(range(0, 256, 17))

    # RTSTRUCT 无像素 -> 422
    rt = _preview(client, job["id"], _anon(meta["rt_sop"]))
    assert rt.status_code == 422
    assert rt.json()["detail"]["reason"] == "missing_pixel_data"


def test_preview_does_not_mutate_anonymized_output(client, tmp_path):
    """重复渲染与全量下载的像素内容不受预览影响。"""
    from tests.sample_data import build_sample_zip

    zip_path = tmp_path / "sample.zip"
    meta = build_sample_zip(str(zip_path), seed=1)
    with open(zip_path, "rb") as fh:
        r = client.post(
            "/jobs",
            files={"file": ("study.zip", fh, "application/zip")},
            data={"secret": SECRET, "shift_dates": "false"},
        )
    job = r.json()

    p1 = _preview(client, job["id"], _anon(meta["sops"][0]))
    p2 = _preview(client, job["id"], _anon(meta["sops"][0]))
    assert p1.content == p2.content

    full = client.get(f"/jobs/{job['id']}/download")
    assert full.status_code == 200
    with zipfile.ZipFile(io.BytesIO(full.content)) as zf:
        assert f"{_anon(meta['sops'][0])}.dcm" in zf.namelist()
