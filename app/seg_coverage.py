"""匿名 BINARY SEG（Segmentation）分割覆盖率统计。

数据来源严格限定为“已完成任务 output 目录中的匿名 DICOM 文件”，
**不重新解压或处理原始 ZIP**，只纳入成功写出的实例。

解析规则：

* 按匿名 SOPInstanceUID 定位唯一成功输出；文件必须是 Segmentation Storage
  （SOPClassUID = 1.2.840.10008.5.1.4.1.1.66.4）、``SegmentationType=BINARY``、
  未压缩 native 传输语法（Implicit/Explicit VR Little Endian、
  Explicit VR Big Endian）、1-bit 位深
  （BitsAllocated=BitsStored=1、HighBit=0）；
* 逐帧读取 ``PerFrameFunctionalGroupsSequence[i]``：
  ``SegmentIdentificationSequence[0].ReferencedSegmentNumber`` 给出段号；
  遍历该帧**全部** ``DerivationImageSequence`` 项及其**全部**
  ``SourceImageSequence`` 项，收集 ``ReferencedSOPInstanceUID``（匿名化已把
  序列内引用与源实例一致重写，因此可直接与任务成功输出对齐）并按匿名 SOP UID
  去重——恰有一个唯一 UID 时该帧归属对应源图像；同一帧出现两个及以上不同
  UID（如多源分割）时无法判定唯一归属，记 ``multiple_source_references`` 冲突，
  不静默归给首个引用；
* 源图像必须在任务成功输出中**唯一**，且其 Rows/Columns 与 SEG 掩码一致；
* 解码 1-bit 掩码，同一（源图像, 段号）的多帧逐像素取**并集**，
  统计前景像素数、图像像素数（源图像 Rows × Columns）与覆盖率，
  结果按（源 SOP UID, 段号）排序。

存在以下任一情况时抛出 409（SEG_COVERAGE_CONFLICT），在 problems[] 中一次性
给出**含匿名 SOP UID** 的全部问题，不输出部分统计：

* 不是 Segmentation（``not_segmentation``）、非 BINARY 分割
  （``unsupported_segmentation_type``）、压缩/封装传输语法
  （``unsupported_transfer_syntax``）、文件内 SOP UID 与请求不符
  （``sop_instance_uid_mismatch``）、匿名输出不可读（``unreadable``）；
* 1-bit 像素元数据非法（``invalid_pixel_metadata``）、无 PixelData
  （``missing_pixel_data``）、像素数据截断/无法解码
  （``pixel_decode_failed``）；
* NumberOfFrames 缺失/非法（``invalid_number_of_frames``）、
  PerFrameFunctionalGroupsSequence 缺失或条目数与声明帧数不符
  （``frame_count_mismatch``）；
* 帧缺少有效 ReferencedSegmentNumber（``missing_segment_number``）或缺少
  DerivationImageSequence/SourceImageSequence 引用
  （``missing_source_reference``）；同一帧引用两个及以上不同匿名 SOP UID
  （``multiple_source_references``，逐帧给出冲突 UID 列表）；
* 引用的源图像不在任务成功输出中（``source_not_in_output``）、对应多份输出
  （``source_ambiguous_output``）、磁盘缺失（``source_output_missing``）、
  不可读或缺少有效尺寸（``source_unreadable``）、尺寸与掩码不一致
  （``source_dimension_mismatch``）。
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError
from pydicom.uid import (
    ExplicitVRLittleEndian,
    ExplicitVRBigEndian,
    ImplicitVRLittleEndian,
)

from .anonymizer import FileReport, JobResult

# Segmentation Storage SOP Class UID
SEGMENTATION_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.66.4"

# 仅接受三种未压缩 native 传输语法；封装语法（JPEG / RLE / JPEG-LS /
# JPEG 2000 等）与 Deflated Explicit VR 一律拒绝
UNCOMPRESSED_TRANSFER_SYNTAXES = frozenset(
    {
        str(ImplicitVRLittleEndian),
        str(ExplicitVRLittleEndian),
        str(ExplicitVRBigEndian),
    }
)

# detail.problems[].reason 取值（机器可读）
REASON_UNREADABLE = "unreadable"
REASON_NOT_SEGMENTATION = "not_segmentation"
REASON_UNSUPPORTED_SEGMENTATION_TYPE = "unsupported_segmentation_type"
REASON_UNSUPPORTED_TRANSFER_SYNTAX = "unsupported_transfer_syntax"
REASON_SOP_UID_MISMATCH = "sop_instance_uid_mismatch"
REASON_INVALID_PIXEL_METADATA = "invalid_pixel_metadata"
REASON_INVALID_NUMBER_OF_FRAMES = "invalid_number_of_frames"
REASON_FRAME_COUNT_MISMATCH = "frame_count_mismatch"
REASON_MISSING_PIXEL_DATA = "missing_pixel_data"
REASON_PIXEL_DECODE_FAILED = "pixel_decode_failed"
REASON_MISSING_SEGMENT_NUMBER = "missing_segment_number"
REASON_MISSING_SOURCE_REFERENCE = "missing_source_reference"
REASON_MULTIPLE_SOURCE_REFERENCES = "multiple_source_references"
REASON_SOURCE_NOT_IN_OUTPUT = "source_not_in_output"
REASON_SOURCE_AMBIGUOUS_OUTPUT = "source_ambiguous_output"
REASON_SOURCE_OUTPUT_MISSING = "source_output_missing"
REASON_SOURCE_UNREADABLE = "source_unreadable"
REASON_SOURCE_DIMENSION_MISMATCH = "source_dimension_mismatch"

# 顶层 detail.reason 取值（非 problems 场景）
REASON_AMBIGUOUS_OUTPUT = "ambiguous_output"
REASON_OUTPUT_MISSING = "output_missing"


class SegCoverageError(Exception):
    """SEG 覆盖率统计失败；http_status 由路由层映射为 HTTPException。"""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        sop_uid: Optional[str] = None,
        reason: Optional[str] = None,
        extra: Optional[dict] = None,
        problems: Optional[list[dict]] = None,
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
        if problems:
            detail["problems"] = problems
        self.detail = detail


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
        raise SegCoverageError(
            "INSTANCE_NOT_FOUND",
            404,
            "该匿名 SOPInstanceUID 在本任务的成功输出中不存在",
            sop_uid=sop_uid,
        )
    if len(matches) > 1:
        output_files = sorted(r.output_name for r in matches)
        raise SegCoverageError(
            "AMBIGUOUS_INSTANCE_OUTPUT",
            409,
            "同一匿名 SOPInstanceUID 对应多份输出文件，无法确定要统计的 SEG",
            sop_uid=sop_uid,
            reason=REASON_AMBIGUOUS_OUTPUT,
            extra={"output_files": output_files},
        )
    return matches[0]


# --------------------------------------------------------------------------- #
# 标签解析
# --------------------------------------------------------------------------- #
def _positive_int(value: object) -> Optional[int]:
    """解析正整数；缺失/无法解析/非正数返回 None。"""
    if value is None:
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _frame_segment_number(fg_item) -> Optional[int]:
    """逐帧功能组 -> SegmentIdentificationSequence[0] -> ReferencedSegmentNumber。"""
    seq = getattr(fg_item, "SegmentIdentificationSequence", None)
    if not seq:
        return None
    return _positive_int(getattr(seq[0], "ReferencedSegmentNumber", None))


def _frame_source_uids(fg_item) -> list[str]:
    """收集逐帧功能组中全部源图像引用的匿名 SOP UID（已去重、按字典序排列）。

    遍历**所有** DerivationImageSequence 项及其**所有** SourceImageSequence 项
    的 ReferencedSOPInstanceUID，而不是只读 [0]/[0]：同一帧引用两个不同匿名
    SOP UID（多源分割）时由调用方按 multiple_source_references 拒绝。
    没有任何非空引用时返回空列表（对应 missing_source_reference）。
    """
    uids: set[str] = set()
    deriv = getattr(fg_item, "DerivationImageSequence", None)
    if deriv:
        for deriv_item in deriv:
            src_seq = getattr(deriv_item, "SourceImageSequence", None)
            if not src_seq:
                continue
            for src_item in src_seq:
                uid = str(
                    getattr(src_item, "ReferencedSOPInstanceUID", "") or ""
                ).strip()
                if uid:
                    uids.add(uid)
    return sorted(uids)


# --------------------------------------------------------------------------- #
# 源图像解析
# --------------------------------------------------------------------------- #
def _resolve_source(
    result: JobResult,
    source_uid: str,
    index: dict[str, list[FileReport]],
    problems: list[dict],
    base: dict,
) -> Optional[tuple[str, int, int]]:
    """把引用的匿名 SOP UID 解析为唯一源图像输出并读取其 Rows/Columns。

    成功返回 (output_file, rows, columns)；失败记录问题并返回 None。
    只读取像素之前的标签（stop_before_pixels），不解码源图像像素。
    """
    problem_base = {**base, "source_sop_instance_uid": source_uid}
    matches = index.get(source_uid, [])
    if not matches:
        problems.append({
            **problem_base,
            "reason": REASON_SOURCE_NOT_IN_OUTPUT,
            "message": "帧引用的源图像匿名 SOP UID 不在本任务的成功输出中",
        })
        return None
    if len(matches) > 1:
        output_files = sorted(r.output_name for r in matches)
        problems.append({
            **problem_base,
            "reason": REASON_SOURCE_AMBIGUOUS_OUTPUT,
            "output_files": output_files,
            "message": (
                "帧引用的源图像匿名 SOP UID 对应多份输出文件，"
                "无法确定唯一源图像"
            ),
        })
        return None

    report = matches[0]
    problem_base["source_output_file"] = report.output_name
    path = os.path.join(result.output_dir, report.output_name)
    if not os.path.isfile(path):
        problems.append({
            **problem_base,
            "reason": REASON_SOURCE_OUTPUT_MISSING,
            "message": "源图像匿名输出文件在任务目录中不存在（可能已被清理）",
        })
        return None

    try:
        ds = pydicom.dcmread(path, force=False, stop_before_pixels=True)
    except (InvalidDicomError, OSError, EOFError) as exc:
        problems.append({
            **problem_base,
            "reason": REASON_SOURCE_UNREADABLE,
            "message": f"源图像匿名输出不是可读取的 DICOM 文件：{exc}",
        })
        return None
    except Exception as exc:  # 标签编码损坏等
        problems.append({
            **problem_base,
            "reason": REASON_SOURCE_UNREADABLE,
            "message": f"读取源图像匿名输出失败：{exc}",
        })
        return None

    rows = _positive_int(getattr(ds, "Rows", None))
    columns = _positive_int(getattr(ds, "Columns", None))
    if rows is None or columns is None:
        problems.append({
            **problem_base,
            "reason": REASON_SOURCE_UNREADABLE,
            "message": "源图像缺少有效的 Rows/Columns，无法核对掩码尺寸",
        })
        return None
    return report.output_name, rows, columns


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def analyze_seg_coverage(job, sop_uid: str) -> dict:
    """按匿名 SOP UID 定位任务成功输出并统计 BINARY SEG 分割覆盖率。

    任何定位/解析/校验错误均抛 SegCoverageError；成功时返回按
    （源 SOP UID, 段号）排序的分组统计（前景像素数 / 图像像素数 / 覆盖率）。
    """
    result: JobResult = job.result
    report = _locate_unique_output(result, sop_uid)
    path = os.path.join(result.output_dir, report.output_name)
    if not os.path.isfile(path):
        raise SegCoverageError(
            "INSTANCE_OUTPUT_MISSING",
            404,
            "匿名输出文件在任务目录中不存在（可能已被清理）",
            sop_uid=sop_uid,
            reason=REASON_OUTPUT_MISSING,
            extra={"output_file": report.output_name},
        )

    try:
        ds = pydicom.dcmread(path, force=False)
    except (InvalidDicomError, OSError, EOFError) as exc:
        raise SegCoverageError(
            "SEG_COVERAGE_CONFLICT",
            409,
            "匿名输出不是可读取的 DICOM 文件，无法统计分割覆盖率",
            sop_uid=sop_uid,
            extra={"conflict_count": 1},
            problems=[{
                "sop_instance_uid": sop_uid,
                "output_file": report.output_name,
                "reason": REASON_UNREADABLE,
                "message": f"匿名输出不是可读取的 DICOM 文件：{exc}",
            }],
        ) from exc
    except Exception as exc:  # 标签编码损坏等
        raise SegCoverageError(
            "SEG_COVERAGE_CONFLICT",
            409,
            "读取匿名输出失败，无法统计分割覆盖率",
            sop_uid=sop_uid,
            extra={"conflict_count": 1},
            problems=[{
                "sop_instance_uid": sop_uid,
                "output_file": report.output_name,
                "reason": REASON_UNREADABLE,
                "message": f"读取匿名输出失败：{exc}",
            }],
        ) from exc

    base = {"sop_instance_uid": sop_uid, "output_file": report.output_name}

    # ---- 文件级校验：任一失败即不再继续帧/像素解析 ------------------------ #
    problems: list[dict] = []

    actual_sop = str(getattr(ds, "SOPInstanceUID", "") or "").strip()
    if actual_sop and actual_sop != sop_uid:
        problems.append({
            **base,
            "reason": REASON_SOP_UID_MISMATCH,
            "message": (
                "文件内 SOPInstanceUID 与请求的匿名 SOP UID 不一致，拒绝统计："
                f"文件内为 {actual_sop}，请求为 {sop_uid}"
            ),
        })

    sop_class = str(getattr(ds, "SOPClassUID", "") or "").strip()
    if sop_class != SEGMENTATION_SOP_CLASS:
        problems.append({
            **base,
            "reason": REASON_NOT_SEGMENTATION,
            "message": (
                "该实例不是 Segmentation Storage（SOPClassUID="
                f"{sop_class or '<缺失>'}），无法统计分割覆盖率"
            ),
        })

    seg_type = str(getattr(ds, "SegmentationType", "") or "").strip().upper()
    if seg_type != "BINARY":
        problems.append({
            **base,
            "reason": REASON_UNSUPPORTED_SEGMENTATION_TYPE,
            "segmentation_type": seg_type or None,
            "message": (
                "仅支持 BINARY 分割，收到 SegmentationType="
                f"{seg_type or '<缺失>'}"
            ),
        })

    ts_uid = str(
        getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", "") or ""
    ).strip()
    if ts_uid not in UNCOMPRESSED_TRANSFER_SYNTAXES:
        problems.append({
            **base,
            "reason": REASON_UNSUPPORTED_TRANSFER_SYNTAX,
            "transfer_syntax_uid": ts_uid or None,
            "message": (
                "仅支持未压缩传输语法（Implicit/Explicit VR Little Endian、"
                f"Explicit VR Big Endian），收到 TransferSyntaxUID="
                f"{ts_uid or '<缺失>'}"
            ),
        })

    if problems:
        raise SegCoverageError(
            "SEG_COVERAGE_CONFLICT",
            409,
            "该实例不是可支持的未压缩 BINARY Segmentation，无法统计分割覆盖率",
            sop_uid=sop_uid,
            extra={"conflict_count": len(problems)},
            problems=problems,
        )

    # ---- 像素元数据与帧数校验 -------------------------------------------- #
    rows = _positive_int(getattr(ds, "Rows", None))
    columns = _positive_int(getattr(ds, "Columns", None))
    bits_allocated = _positive_int(getattr(ds, "BitsAllocated", None))
    bits_stored = _positive_int(getattr(ds, "BitsStored", None))
    high_bit_raw = getattr(ds, "HighBit", None)
    try:
        high_bit: Optional[int] = int(str(high_bit_raw).strip())
    except (TypeError, ValueError):
        high_bit = None
    if (
        rows is None
        or columns is None
        or bits_allocated != 1
        or bits_stored != 1
        or high_bit != 0
    ):
        problems.append({
            **base,
            "reason": REASON_INVALID_PIXEL_METADATA,
            "message": (
                "BINARY SEG 需满足 Rows/Columns 为正整数、"
                "BitsAllocated=BitsStored=1、HighBit=0，收到 "
                f"Rows={getattr(ds, 'Rows', None)!r}、"
                f"Columns={getattr(ds, 'Columns', None)!r}、"
                f"BitsAllocated={getattr(ds, 'BitsAllocated', None)!r}、"
                f"BitsStored={getattr(ds, 'BitsStored', None)!r}、"
                f"HighBit={high_bit_raw!r}"
            ),
        })

    frames_raw = getattr(ds, "NumberOfFrames", None)
    frames = _positive_int(frames_raw)
    if frames is None:
        problems.append({
            **base,
            "reason": REASON_INVALID_NUMBER_OF_FRAMES,
            "declared_number_of_frames": (
                "" if frames_raw is None else str(frames_raw)
            ),
            "message": (
                "NumberOfFrames 缺失或取值非法，无法确定帧数："
                f"{frames_raw!r}"
            ),
        })

    per_frame_seq = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
    if frames is not None and (
        per_frame_seq is None or len(per_frame_seq) != frames
    ):
        problems.append({
            **base,
            "reason": REASON_FRAME_COUNT_MISMATCH,
            "declared_number_of_frames": str(frames),
            "per_frame_item_count": (
                0 if per_frame_seq is None else len(per_frame_seq)
            ),
            "message": (
                f"NumberOfFrames 声明 {frames} 帧，但 "
                f"PerFrameFunctionalGroupsSequence "
                f"{'缺失' if per_frame_seq is None else f'含 {len(per_frame_seq)} 个条目'}"
                "，无法逐帧对应"
            ),
        })

    if "PixelData" not in ds or not ds.PixelData:
        problems.append({
            **base,
            "reason": REASON_MISSING_PIXEL_DATA,
            "message": "SEG 不包含 PixelData，无法解码 1-bit 掩码",
        })

    if problems:
        raise SegCoverageError(
            "SEG_COVERAGE_CONFLICT",
            409,
            "SEG 的帧数声明或像素元数据异常，无法统计分割覆盖率",
            sop_uid=sop_uid,
            extra={"conflict_count": len(problems)},
            problems=problems,
        )

    # ---- 解码 1-bit 掩码 --------------------------------------------------- #
    try:
        arr = np.asarray(ds.pixel_array)
    except Exception as exc:  # 截断 / 字节数与元数据不符等 native 解码失败
        raise SegCoverageError(
            "SEG_COVERAGE_CONFLICT",
            409,
            "SEG 像素数据异常，无法解码 1-bit 掩码",
            sop_uid=sop_uid,
            extra={"conflict_count": 1},
            problems=[{
                **base,
                "reason": REASON_PIXEL_DECODE_FAILED,
                "message": f"1-bit 掩码解码失败：{exc}",
            }],
        ) from exc

    if arr.ndim == 2:
        # NumberOfFrames=1 时 pydicom 返回二维数组
        arr = arr[np.newaxis, ...]
    if arr.ndim != 3 or arr.shape != (frames, rows, columns):
        raise SegCoverageError(
            "SEG_COVERAGE_CONFLICT",
            409,
            "SEG 像素数据异常，无法解码 1-bit 掩码",
            sop_uid=sop_uid,
            extra={"conflict_count": 1},
            problems=[{
                **base,
                "reason": REASON_PIXEL_DECODE_FAILED,
                "message": (
                    f"解码后掩码维度 {tuple(arr.shape)} 与 "
                    f"(NumberOfFrames={frames}, Rows={rows}, "
                    f"Columns={columns}) 不一致"
                ),
            }],
        )
    masks = arr != 0

    # ---- 逐帧提取（段号, 源图像匿名 UID） ---------------------------------- #
    # 逐帧遍历全部 DerivationImageSequence/SourceImageSequence 引用并按匿名
    # SOP UID 去重：恰有一个唯一 UID 时该帧归属该源图像；同一帧引用多个不同
    # UID 时记 multiple_source_references，不静默归给第一张源图像。
    # (frame_index, segment_number, source_sop_instance_uid)
    frame_refs: list[tuple[int, int, str]] = []
    for frame_index in range(frames):
        fg_item = per_frame_seq[frame_index]
        seg_num = _frame_segment_number(fg_item)
        if seg_num is None:
            problems.append({
                **base,
                "frame_index": frame_index,
                "reason": REASON_MISSING_SEGMENT_NUMBER,
                "message": (
                    f"帧 {frame_index} 缺少有效的 "
                    "SegmentIdentificationSequence/ReferencedSegmentNumber"
                ),
            })
        source_uids = _frame_source_uids(fg_item)
        if not source_uids:
            problems.append({
                **base,
                "frame_index": frame_index,
                "reason": REASON_MISSING_SOURCE_REFERENCE,
                "message": (
                    f"帧 {frame_index} 缺少 DerivationImageSequence/"
                    "SourceImageSequence 的 ReferencedSOPInstanceUID 引用"
                ),
            })
        elif len(source_uids) > 1:
            problems.append({
                **base,
                "frame_index": frame_index,
                "reason": REASON_MULTIPLE_SOURCE_REFERENCES,
                "source_sop_instance_uids": source_uids,
                "message": (
                    f"帧 {frame_index} 在 DerivationImageSequence/"
                    "SourceImageSequence 中引用了多个不同的源图像匿名 "
                    f"SOP UID（{len(source_uids)} 个），无法唯一确定该帧的"
                    "源图像，拒绝把该帧静默归给首个引用："
                    f"{', '.join(source_uids)}"
                ),
            })
        elif seg_num is not None:
            frame_refs.append((frame_index, seg_num, source_uids[0]))

    # ---- 解析被引用源图像（唯一、可读、尺寸一致） -------------------------- #
    index: dict[str, list[FileReport]] = {}
    for r in _successful_reports(result):
        index.setdefault(r.sop_instance_uid, []).append(r)

    # source_uid -> (output_file, rows, columns)，仅收录尺寸与掩码一致的源图像
    source_dims: dict[str, tuple[str, int, int]] = {}
    for source_uid in sorted({ref[2] for ref in frame_refs}):
        resolved = _resolve_source(result, source_uid, index, problems, base)
        if resolved is None:
            continue
        output_file, src_rows, src_columns = resolved
        if src_rows != rows or src_columns != columns:
            problems.append({
                **base,
                "source_sop_instance_uid": source_uid,
                "source_output_file": output_file,
                "reason": REASON_SOURCE_DIMENSION_MISMATCH,
                "source_rows": src_rows,
                "source_columns": src_columns,
                "message": (
                    f"源图像尺寸 ({src_rows} x {src_columns}) 与 SEG 掩码 "
                    f"({rows} x {columns}) 不一致"
                ),
            })
            continue
        source_dims[source_uid] = (output_file, src_rows, src_columns)

    if problems:
        raise SegCoverageError(
            "SEG_COVERAGE_CONFLICT",
            409,
            "SEG 帧引用缺失/歧义或源图像尺寸不一致，"
            "无法给出完整的分割覆盖率统计（不输出部分统计）",
            sop_uid=sop_uid,
            extra={"conflict_count": len(problems)},
            problems=problems,
        )

    # ---- 同（源图像, 段号）多帧逐像素并集 ---------------------------------- #
    unions: dict[tuple[str, int], np.ndarray] = {}
    frame_counts: dict[tuple[str, int], int] = {}
    for frame_index, seg_num, source_uid in frame_refs:
        key = (source_uid, seg_num)
        union = unions.get(key)
        if union is None:
            unions[key] = masks[frame_index].copy()
            frame_counts[key] = 1
        else:
            union |= masks[frame_index]
            frame_counts[key] += 1

    groups = []
    for source_uid, seg_num in sorted(unions):
        output_file, src_rows, src_columns = source_dims[source_uid]
        image_pixels = src_rows * src_columns
        foreground = int(unions[(source_uid, seg_num)].sum())
        groups.append({
            "source_sop_instance_uid": source_uid,
            "source_output_file": output_file,
            "segment_number": seg_num,
            "frame_count": frame_counts[(source_uid, seg_num)],
            "foreground_pixels": foreground,
            "image_pixels": image_pixels,
            "coverage": foreground / image_pixels,
        })

    return {
        "sop_instance_uid": sop_uid,
        "output_file": report.output_name,
        "segmentation_type": "BINARY",
        "rows": rows,
        "columns": columns,
        "number_of_frames": frames,
        "group_count": len(groups),
        "groups": groups,
    }
