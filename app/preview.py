"""按匿名 SOPInstanceUID 从成功输出渲染单帧灰度 PNG 预览（供人工核对影像内容）。

数据来源严格限定为“已完成任务 output_dir 中的匿名 DICOM 文件”，
**不重新解压或处理原始 ZIP**，匿名化不改动像素字节，因此预览即匿名结果的实际影像。

支持范围（其余影像明确报错，不静默返回错误内容）：

* 未压缩（native）传输语法：Implicit/Explicit VR Little Endian、
  Explicit VR Big Endian；封装像素数据（JPEG / RLE / JPEG-LS / JPEG 2000 等）
  与 Deflated Explicit VR 一律拒绝；
* 单帧（NumberOfFrames 缺省或为 1）；
* SamplesPerPixel=1、PhotometricInterpretation=MONOCHROME1/MONOCHROME2；
* BitsAllocated=8/16（BitsStored/HighBit/PixelRepresentation 按 DICOM 标准取值，
  pydicom 负责按 BitsStored 掩码与符号扩展）。

渲染管线：

1. 应用 Modality LUT 线性映射：``value = pixel * RescaleSlope + RescaleIntercept``
   （缺省 slope=1、intercept=0）；
2. 按 WindowCenter/WindowWidth 的**首项**做 DICOM 标准线性 VOI 映射到 8 位灰度；
   缺少窗宽窗位时按像素最小/最大值线性拉伸；
3. MONOCHROME1 在最后做灰度反相（值越大越暗）。
"""
from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pydicom
from PIL import Image
from pydicom.errors import InvalidDicomError
from pydicom.uid import (
    ExplicitVRLittleEndian,
    ExplicitVRBigEndian,
    ImplicitVRLittleEndian,
)

from .anonymizer import FileReport, JobResult

# 仅接受三种未压缩 native 传输语法；Deflated Explicit VR（zlib 压缩，
# is_compressed=False 但数据集整体被 deflate）同样不属于“未压缩”，拒绝。
UNCOMPRESSED_TRANSFER_SYNTAXES = frozenset(
    {
        str(ImplicitVRLittleEndian),
        str(ExplicitVRLittleEndian),
        str(ExplicitVRBigEndian),
    }
)

SUPPORTED_PHOTOMETRICS = frozenset({"MONOCHROME1", "MONOCHROME2"})

# detail.reason 取值（机器可读）
REASON_AMBIGUOUS_OUTPUT = "ambiguous_output"
REASON_OUTPUT_MISSING = "output_missing"
REASON_UNREADABLE = "unreadable"
REASON_UNSUPPORTED_TRANSFER_SYNTAX = "unsupported_transfer_syntax"
REASON_UNSUPPORTED_SAMPLES = "unsupported_samples_per_pixel"
REASON_UNSUPPORTED_PHOTOMETRIC = "unsupported_photometric_interpretation"
REASON_MULTIFRAME = "multiframe"
REASON_UNSUPPORTED_BIT_DEPTH = "unsupported_bit_depth"
REASON_INVALID_PIXEL_METADATA = "invalid_pixel_metadata"
REASON_MISSING_PIXEL_DATA = "missing_pixel_data"
REASON_PIXEL_DECODE_FAILED = "pixel_decode_failed"
REASON_INVALID_RESCALE = "invalid_rescale"
REASON_INVALID_VOI_WINDOW = "invalid_voi_window"
REASON_SOP_UID_MISMATCH = "sop_instance_uid_mismatch"


class PreviewError(Exception):
    """预览渲染失败；http_status 由路由层映射为 HTTPException。"""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        sop_uid: Optional[str] = None,
        reason: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        detail: dict = {"code": code, "message": message}
        if sop_uid is not None:
            detail["sop_instance_uid"] = sop_uid
        if reason is not None:
            detail["reason"] = reason
        if extra:
            detail.update(extra)
        self.detail = detail


@dataclass
class RenderedPreview:
    png: bytes
    rows: int
    columns: int
    bits_allocated: int
    bits_stored: int
    photometric_interpretation: str
    transfer_syntax_uid: str
    rescale_slope: float
    rescale_intercept: float
    window_source: str  # voi（使用 WindowCenter/Width 首项） | minmax（像素极值拉伸）


# --------------------------------------------------------------------------- #
# 输出定位
# --------------------------------------------------------------------------- #
def _successful_reports(result: JobResult) -> list[FileReport]:
    """任务内真正写出匿名文件、且记录了匿名 SOP UID 的报告。"""
    return [
        report
        for report in result.files
        if report.status != "error" and report.output_name and report.sop_instance_uid
    ]


def _locate_unique_output(result: JobResult, sop_uid: str) -> FileReport:
    """按匿名 SOPInstanceUID 定位唯一成功输出；找不到 / 多份输出均明确报错。"""
    matches = [
        report
        for report in _successful_reports(result)
        if report.sop_instance_uid == sop_uid
    ]
    if not matches:
        raise PreviewError(
            "INSTANCE_NOT_FOUND",
            404,
            "该匿名 SOPInstanceUID 在本任务的成功输出中不存在",
            sop_uid=sop_uid,
        )
    if len(matches) > 1:
        output_files = sorted(r.output_name for r in matches)
        raise PreviewError(
            "AMBIGUOUS_INSTANCE_OUTPUT",
            409,
            "同一匿名 SOPInstanceUID 对应多份输出文件，无法确定要预览的影像",
            sop_uid=sop_uid,
            reason=REASON_AMBIGUOUS_OUTPUT,
            extra={"output_files": output_files},
        )
    return matches[0]


# --------------------------------------------------------------------------- #
# 标签取值
# --------------------------------------------------------------------------- #
def _first_float(value: object) -> Optional[float]:
    """取单值 / VM>1（MultiValue 或反斜杠字符串）的首项并转为 float。

    元素缺省或为空返回 None；值存在但无法解析时抛出 ValueError。
    """
    if value is None:
        return None
    if isinstance(value, pydicom.multival.MultiValue):
        if not value:
            return None
        raw = value[0]
    elif isinstance(value, (list, tuple)):
        if not value:
            return None
        raw = value[0]
    else:
        raw = value
    text = str(raw).strip()
    if not text:
        return None
    return float(text)


def _positive_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


# --------------------------------------------------------------------------- #
# 影像校验
# --------------------------------------------------------------------------- #
def _read_anonymized(path: str):
    """匿名化已强制写出 File Meta，这里要求严格 DICOM 解析。"""
    return pydicom.dcmread(path, force=False)


def _validate_image_shape(ds, sop_uid: str) -> tuple[int, int, int, int, int, str]:
    """校验并返回 (rows, columns, bits_allocated, bits_stored, pixel_repr, photometric)。"""
    rows = _positive_int(getattr(ds, "Rows", None))
    columns = _positive_int(getattr(ds, "Columns", None))
    if rows is None or columns is None:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            "影像缺少有效的 Rows/Columns，无法渲染预览",
            sop_uid=sop_uid,
            reason=REASON_INVALID_PIXEL_METADATA,
        )

    samples = getattr(ds, "SamplesPerPixel", None)
    try:
        samples = int(samples)
    except (TypeError, ValueError):
        samples = None
    if samples != 1:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            f"预览仅支持 SamplesPerPixel=1 的灰度影像，收到 {samples!r}",
            sop_uid=sop_uid,
            reason=REASON_UNSUPPORTED_SAMPLES,
        )

    photometric = str(getattr(ds, "PhotometricInterpretation", "") or "").strip().upper()
    if photometric not in SUPPORTED_PHOTOMETRICS:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            "预览仅支持 MONOCHROME1/MONOCHROME2 灰度影像，"
            f"收到 PhotometricInterpretation={photometric or '<缺失>'}",
            sop_uid=sop_uid,
            reason=REASON_UNSUPPORTED_PHOTOMETRIC,
        )

    # NumberOfFrames 缺省视为单帧；显式 >1 拒绝
    frames_raw = getattr(ds, "NumberOfFrames", None)
    if frames_raw is not None and str(frames_raw).strip():
        frames = _positive_int(frames_raw)
        if frames is None or frames != 1:
            raise PreviewError(
                "PREVIEW_UNSUPPORTED_IMAGE",
                422,
                f"预览仅支持单帧影像，收到 NumberOfFrames={frames_raw}",
                sop_uid=sop_uid,
                reason=REASON_MULTIFRAME,
            )

    bits_allocated = getattr(ds, "BitsAllocated", None)
    try:
        bits_allocated = int(bits_allocated)
    except (TypeError, ValueError):
        bits_allocated = None
    bits_stored = getattr(ds, "BitsStored", None)
    try:
        bits_stored = int(bits_stored)
    except (TypeError, ValueError):
        bits_stored = None
    high_bit = getattr(ds, "HighBit", None)
    try:
        high_bit = int(high_bit)
    except (TypeError, ValueError):
        high_bit = None
    pixel_repr = getattr(ds, "PixelRepresentation", None)
    try:
        pixel_repr = int(pixel_repr)
    except (TypeError, ValueError):
        pixel_repr = None

    if bits_allocated not in (8, 16):
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            f"预览仅支持 BitsAllocated=8/16 的未压缩灰度影像，收到 {bits_allocated!r}",
            sop_uid=sop_uid,
            reason=REASON_UNSUPPORTED_BIT_DEPTH,
        )
    if (
        bits_stored is None
        or not (1 <= bits_stored <= bits_allocated)
        or high_bit != bits_stored - 1
        or pixel_repr not in (0, 1)
    ):
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            "像素位深元数据无效（需满足 1 <= BitsStored <= BitsAllocated、"
            f"HighBit = BitsStored-1、PixelRepresentation 为 0/1），收到 "
            f"BitsStored={bits_stored!r}、HighBit={high_bit!r}、"
            f"PixelRepresentation={pixel_repr!r}",
            sop_uid=sop_uid,
            reason=REASON_INVALID_PIXEL_METADATA,
        )

    return rows, columns, bits_allocated, bits_stored, pixel_repr, photometric


def _resolve_rescale(ds, sop_uid: str) -> tuple[float, float]:
    """读取 RescaleSlope/Intercept（缺省 1/0）；存在但非法时明确报错。"""
    try:
        slope = _first_float(getattr(ds, "RescaleSlope", None))
        intercept = _first_float(getattr(ds, "RescaleIntercept", None))
    except (ValueError, OverflowError) as exc:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            f"RescaleSlope/RescaleIntercept 无法解析：{exc}",
            sop_uid=sop_uid,
            reason=REASON_INVALID_RESCALE,
        ) from exc
    if slope is None:
        slope = 1.0
    if intercept is None:
        intercept = 0.0
    if not math.isfinite(slope) or not math.isfinite(intercept) or slope == 0.0:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            f"RescaleSlope/RescaleIntercept 取值非法（slope 不可为 0 或无穷）："
            f"slope={slope}, intercept={intercept}",
            sop_uid=sop_uid,
            reason=REASON_INVALID_RESCALE,
        )
    return slope, intercept


def _resolve_window(ds, sop_uid: str) -> tuple[Optional[float], Optional[float]]:
    """读取 WindowCenter/Width 首项；两者任一缺省走极值拉伸，存在但非法则报错。"""
    center_el = getattr(ds, "WindowCenter", None)
    width_el = getattr(ds, "WindowWidth", None)
    if center_el is None and width_el is None:
        return None, None
    try:
        center = _first_float(center_el)
        width = _first_float(width_el)
    except (ValueError, OverflowError) as exc:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            f"WindowCenter/WindowWidth 无法解析：{exc}",
            sop_uid=sop_uid,
            reason=REASON_INVALID_VOI_WINDOW,
        ) from exc
    if center is None or width is None:
        # 只有其中一个标签（或值为空）：视为缺窗，按极值拉伸
        return None, None
    if not math.isfinite(center) or not math.isfinite(width) or width <= 0:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            f"WindowCenter/WindowWidth 取值非法（Width 必须为正数）："
            f"center={center}, width={width}",
            sop_uid=sop_uid,
            reason=REASON_INVALID_VOI_WINDOW,
        )
    return center, width


# --------------------------------------------------------------------------- #
# 灰度映射
# --------------------------------------------------------------------------- #
def _apply_window(values: np.ndarray, center: float, width: float) -> np.ndarray:
    """DICOM PS3.3 C.11.2.1.2 标准线性 VOI：输出 0..1 浮点灰度。"""
    if width <= 1.0:
        # 宽度退化为 1 时分母为 0：按窗位二值化
        scaled = np.where(values >= center, 1.0, 0.0)
    else:
        scaled = (values - (center - 0.5)) / (width - 1.0) + 0.5
    return np.clip(scaled, 0.0, 1.0)


def _stretch_minmax(values: np.ndarray) -> np.ndarray:
    """缺窗时按像素极值线性拉伸到 0..1；常量影像映射为全黑。"""
    vmin = float(values.min())
    vmax = float(values.max())
    if vmax <= vmin:
        return np.zeros_like(values)
    return np.clip((values - vmin) / (vmax - vmin), 0.0, 1.0)


def _decode_pixels(ds, rows: int, columns: int, sop_uid: str) -> np.ndarray:
    """解码 native 像素数据为二维整数数组（pydicom 负责 BitsStored 掩码/符号扩展）。

    调用前必须已经确认 ``PixelData`` 元素存在（见 :func:`_require_pixel_data`）。
    """
    try:
        raw = ds.pixel_array
    except Exception as exc:  # 截断 / 编码与元数据不符等 native 解码失败
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            f"像素数据无法解码：{exc}",
            sop_uid=sop_uid,
            reason=REASON_PIXEL_DECODE_FAILED,
        ) from exc

    arr = np.asarray(raw)
    if arr.ndim == 3 and arr.shape[0] == 1:
        # NumberOfFrames=1 时个别处理器保留帧维
        arr = arr[0]
    if arr.ndim != 2 or arr.shape != (rows, columns):
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            f"解码后像素维度 {arr.shape} 与 Rows/Columns ({rows}, {columns}) 不一致",
            sop_uid=sop_uid,
            reason=REASON_INVALID_PIXEL_METADATA,
        )
    return arr


def _require_pixel_data(ds, sop_uid: str) -> None:
    """先确认存在非空 PixelData（非影像对象如 RTSTRUCT 在此明确拒绝）。"""
    if "PixelData" not in ds or not ds.PixelData:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            "影像不包含 PixelData（可能是非影像对象，如 RTSTRUCT）",
            sop_uid=sop_uid,
            reason=REASON_MISSING_PIXEL_DATA,
        )


def render_preview_png(job, sop_uid: str) -> RenderedPreview:
    """按匿名 SOP UID 定位任务成功输出并渲染 PNG；任何歧义/不支持均抛 PreviewError。"""
    result: JobResult = job.result
    report = _locate_unique_output(result, sop_uid)
    path = os.path.join(result.output_dir, report.output_name)
    if not os.path.isfile(path):
        raise PreviewError(
            "INSTANCE_OUTPUT_MISSING",
            404,
            "匿名输出文件在任务目录中不存在（可能已被清理）",
            sop_uid=sop_uid,
            reason=REASON_OUTPUT_MISSING,
            extra={"output_file": report.output_name},
        )

    try:
        ds = _read_anonymized(path)
    except (InvalidDicomError, OSError, EOFError) as exc:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            f"匿名输出不是可读取的 DICOM 文件：{exc}",
            sop_uid=sop_uid,
            reason=REASON_UNREADABLE,
            extra={"output_file": report.output_name},
        ) from exc
    except Exception as exc:  # 标签编码损坏等
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            f"读取匿名输出失败：{exc}",
            sop_uid=sop_uid,
            reason=REASON_UNREADABLE,
            extra={"output_file": report.output_name},
        ) from exc

    actual_sop = str(getattr(ds, "SOPInstanceUID", "") or "").strip()
    if actual_sop and actual_sop != sop_uid:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            "文件内 SOPInstanceUID 与请求的匿名 SOP UID 不一致，拒绝渲染："
            f"文件内为 {actual_sop}，请求为 {sop_uid}",
            sop_uid=sop_uid,
            reason=REASON_SOP_UID_MISMATCH,
            extra={"output_file": report.output_name},
        )

    ts_uid = str(
        getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", "") or ""
    ).strip()
    if ts_uid not in UNCOMPRESSED_TRANSFER_SYNTAXES:
        raise PreviewError(
            "PREVIEW_UNSUPPORTED_IMAGE",
            422,
            "预览仅支持未压缩传输语法（Implicit/Explicit VR Little Endian、"
            f"Explicit VR Big Endian），收到 TransferSyntaxUID={ts_uid or '<缺失>'}",
            sop_uid=sop_uid,
            reason=REASON_UNSUPPORTED_TRANSFER_SYNTAX,
            extra={"transfer_syntax_uid": ts_uid},
        )

    # 先确认存在像素数据（非影像对象如 RTSTRUCT 在此明确拒绝），
    # 再做 Rows/Columns/位深/帧数等影像元数据校验
    _require_pixel_data(ds, sop_uid)
    rows, columns, bits_allocated, bits_stored, _pixel_repr, photometric = (
        _validate_image_shape(ds, sop_uid)
    )
    slope, intercept = _resolve_rescale(ds, sop_uid)
    center, width = _resolve_window(ds, sop_uid)

    pixels = _decode_pixels(ds, rows, columns, sop_uid).astype(np.float64)
    # 1) Rescale Slope/Intercept（Modality LUT 线性形式）
    values = pixels * slope + intercept

    # 2) VOI 窗（多值取首项）或缺窗极值拉伸
    if center is not None:
        gray_float = _apply_window(values, center, width)
        window_source = "voi"
    else:
        gray_float = _stretch_minmax(values)
        window_source = "minmax"

    gray = np.rint(gray_float * 255.0).astype(np.uint8)

    # 3) MONOCHROME1：像素值越大越暗，最后反相
    if photometric == "MONOCHROME1":
        gray = 255 - gray

    buf = io.BytesIO()
    Image.fromarray(gray, mode="L").save(buf, format="PNG")

    return RenderedPreview(
        png=buf.getvalue(),
        rows=rows,
        columns=columns,
        bits_allocated=bits_allocated,
        bits_stored=bits_stored,
        photometric_interpretation=photometric,
        transfer_syntax_uid=ts_uid,
        rescale_slope=slope,
        rescale_intercept=intercept,
        window_source=window_source,
    )
