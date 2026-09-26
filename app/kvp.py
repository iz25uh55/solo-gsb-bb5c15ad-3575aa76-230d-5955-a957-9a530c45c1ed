"""系列 CT 管电压（KVP）逐帧分布统计。

数据来源严格限定为“已完成任务 output 目录中的匿名 DICOM 文件”，
**不重新解压或处理原始 ZIP**，只纳入成功写出的实例（含多帧增强 CT）。

逐帧 KVP 取值优先级（对应增强 CT 的功能组结构）：

1. 该帧对应的 ``PerFrameFunctionalGroupsSequence[i]`` 内
   ``CTExposureSequence[0].KVP``；
2. ``SharedFunctionalGroupsSequence[0].CTExposureSequence[0].KVP``；
3. 顶层 ``KVP``（0018,0060）。

``NumberOfFrames`` 缺省或为 1 的实例按单帧（1 帧）处理。某一层级的
KVP 标签存在且非空即采用该层级的值；无法解析为有限数值时该帧计入
缺失（``missing_kvp_frames``），**不再向下回退**，避免用其他层级的
值掩盖损坏数据。

存在以下任一情况时返回 ``409 SERIES_KVP_CONFLICT``，在 ``problems[]``
中一次性给出**含匿名 SOP UID** 的全部冲突明细，不给出不完整统计：

* ``NumberOfFrames`` 声明的帧数与 ``PerFrameFunctionalGroupsSequence``
  条目数不一致（``frame_count_mismatch``）；
* ``NumberOfFrames`` 取值非法、无法确定帧数（``invalid_number_of_frames``）；
* 同一匿名 SOPInstanceUID 对应多份物理输出（``ambiguous_output``）；
* 匿名输出不可读，或任务记录有输出但磁盘文件已不存在
  （``unreadable`` / ``output_missing``）。
"""
from __future__ import annotations

import math
import os
from typing import Optional

import pydicom
from pydicom.errors import InvalidDicomError

from .anonymizer import FileReport, JobResult

# detail.problems[].reason 取值（机器可读）
REASON_UNREADABLE = "unreadable"
REASON_OUTPUT_MISSING = "output_missing"
REASON_AMBIGUOUS_OUTPUT = "ambiguous_output"
REASON_FRAME_COUNT_MISMATCH = "frame_count_mismatch"
REASON_INVALID_NUMBER_OF_FRAMES = "invalid_number_of_frames"

# 逐帧 KVP 来源（实例明细 kvp_source 取值）
SOURCE_PER_FRAME = "per_frame"
SOURCE_SHARED = "shared"
SOURCE_TOP_LEVEL = "top_level"
SOURCE_MIXED = "mixed"
SOURCE_NONE = "none"

# 该层级未提供 KVP 的哨兵（与“提供了但无法解析”的 None 区分）
_NOT_PROVIDED = object()


class KvpError(Exception):
    """系列 KVP 统计失败；http_status 由路由层映射为 HTTPException。"""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        extra: Optional[dict] = None,
        problems: Optional[list[dict]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        detail: dict = {"code": code, "message": message}
        if extra:
            detail.update(extra)
        if problems:
            detail["problems"] = problems
        self.detail = detail


# --------------------------------------------------------------------------- #
# 输出收集
# --------------------------------------------------------------------------- #
def _successful_reports(result: JobResult) -> list[FileReport]:
    """任务内真正写出匿名文件、且记录了匿名 SOP UID 的报告。"""
    return [
        report
        for report in result.files
        if report.status != "error" and report.output_name and report.sop_instance_uid
    ]


def _series_reports(result: JobResult, series_uid: str) -> list[FileReport]:
    return [
        report
        for report in _successful_reports(result)
        if report.series_instance_uid == series_uid
    ]


def _detect_ambiguous_outputs(reports: list[FileReport]) -> list[dict]:
    """同一匿名 SOPInstanceUID 对应多份物理输出时逐个 UID 报歧义。"""
    by_sop: dict[str, list[str]] = {}
    for report in reports:
        by_sop.setdefault(report.sop_instance_uid, []).append(report.output_name)

    problems: list[dict] = []
    for sop_uid in sorted(by_sop):
        names = sorted(by_sop[sop_uid])
        if len(names) > 1:
            problems.append({
                "sop_instance_uid": sop_uid,
                "output_file": names[0],
                "output_files": names,
                "reason": REASON_AMBIGUOUS_OUTPUT,
                "message": (
                    "同一匿名 SOPInstanceUID 对应多份输出文件，"
                    "无法确定用于 KVP 统计的唯一实例"
                ),
            })
    return problems


# --------------------------------------------------------------------------- #
# 标签解析
# --------------------------------------------------------------------------- #
def _declared_frames(ds) -> int:
    """声明帧数：NumberOfFrames 缺省/为空按单帧（1）；非法取值抛 ValueError。"""
    raw = getattr(ds, "NumberOfFrames", None)
    if raw is None or not str(raw).strip():
        return 1
    text = str(raw).strip()
    try:
        frames = int(text)
    except (TypeError, ValueError):
        raise ValueError(f"NumberOfFrames 取值非法：{text!r}")
    if frames < 1:
        raise ValueError(f"NumberOfFrames 必须为正整数，收到：{text!r}")
    return frames


def _kvp_value_of(dataset) -> object:
    """读取数据集上的 KVP 标签。

    返回 ``_NOT_PROVIDED``（标签缺失或为空）、解析后的有限 ``float``，
    或 ``None``（标签存在但无法解析为有限数值，该帧计入缺失）。
    """
    raw = getattr(dataset, "KVP", None)
    if raw is None or not str(raw).strip():
        return _NOT_PROVIDED
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _functional_group_kvp(fg_item) -> object:
    """功能组项 -> CTExposureSequence[0] -> KVP（同 _kvp_value_of 的三态约定）。"""
    seq = getattr(fg_item, "CTExposureSequence", None)
    if not seq:
        return _NOT_PROVIDED
    return _kvp_value_of(seq[0])


def _extract_frame_kvps(ds, declared: int) -> tuple[list[Optional[float]], list[str]]:
    """按优先级逐帧解析 KVP，返回 (每帧 KVP 或 None, 每帧来源或空串)。"""
    per_frame_seq = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
    shared_seq = getattr(ds, "SharedFunctionalGroupsSequence", None)
    shared_item = shared_seq[0] if shared_seq else None

    values: list[Optional[float]] = []
    sources: list[str] = []
    for index in range(declared):
        value: object = _NOT_PROVIDED
        source = ""
        if per_frame_seq is not None:
            value = _functional_group_kvp(per_frame_seq[index])
            if value is not _NOT_PROVIDED:
                source = SOURCE_PER_FRAME
        if value is _NOT_PROVIDED and shared_item is not None:
            value = _functional_group_kvp(shared_item)
            if value is not _NOT_PROVIDED:
                source = SOURCE_SHARED
        if value is _NOT_PROVIDED:
            value = _kvp_value_of(ds)
            if value is not _NOT_PROVIDED:
                source = SOURCE_TOP_LEVEL
        # _NOT_PROVIDED（三层都没有）与 None（存在但无法解析）都计为缺失
        values.append(value if isinstance(value, float) else None)
        sources.append(source)
    return values, sources


def _instance_source(sources: list[str]) -> str:
    """实例级 KVP 来源汇总：全部有值帧同源 -> 该来源；多源 -> mixed；无值 -> none。"""
    distinct = {source for source in sources if source}
    if not distinct:
        return SOURCE_NONE
    if len(distinct) > 1:
        return SOURCE_MIXED
    return next(iter(distinct))


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def analyze_series_kvp(job, series_uid: str) -> dict:
    """读取系列成功输出并逐帧统计 KVP 分布；任何错误均抛 KvpError。"""
    result: JobResult = job.result
    reports = _series_reports(result, series_uid)
    if not reports:
        raise KvpError(
            "SERIES_NOT_FOUND",
            404,
            "该匿名 SeriesInstanceUID 在本任务的成功输出中不存在",
            extra={"series_instance_uid": series_uid},
        )

    problems: list[dict] = _detect_ambiguous_outputs(reports)
    ambiguous_sops = {p["sop_instance_uid"] for p in problems}

    instances: list[dict] = []

    for report in sorted(reports, key=lambda r: r.output_name):
        if report.sop_instance_uid in ambiguous_sops:
            continue
        path = os.path.join(result.output_dir, report.output_name)
        if not os.path.isfile(path):
            problems.append({
                "sop_instance_uid": report.sop_instance_uid,
                "output_file": report.output_name,
                "reason": REASON_OUTPUT_MISSING,
                "message": "匿名输出文件在任务目录中不存在（可能已被清理）",
            })
            continue

        try:
            ds = pydicom.dcmread(path, force=False)
        except (InvalidDicomError, OSError, EOFError) as exc:
            problems.append({
                "sop_instance_uid": report.sop_instance_uid,
                "output_file": report.output_name,
                "reason": REASON_UNREADABLE,
                "message": f"匿名输出不是可读取的 DICOM 文件：{exc}",
            })
            continue
        except Exception as exc:  # 标签编码损坏等
            problems.append({
                "sop_instance_uid": report.sop_instance_uid,
                "output_file": report.output_name,
                "reason": REASON_UNREADABLE,
                "message": f"读取匿名输出失败：{exc}",
            })
            continue

        try:
            declared = _declared_frames(ds)
        except ValueError as exc:
            raw_nof = getattr(ds, "NumberOfFrames", None)
            problems.append({
                "sop_instance_uid": report.sop_instance_uid,
                "output_file": report.output_name,
                "reason": REASON_INVALID_NUMBER_OF_FRAMES,
                "declared_number_of_frames": (
                    "" if raw_nof is None else str(raw_nof)
                ),
                "message": f"NumberOfFrames 取值非法，无法确定帧数：{exc}",
            })
            continue

        per_frame_seq = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
        if per_frame_seq is not None and len(per_frame_seq) != declared:
            problems.append({
                "sop_instance_uid": report.sop_instance_uid,
                "output_file": report.output_name,
                "reason": REASON_FRAME_COUNT_MISMATCH,
                "declared_number_of_frames": str(declared),
                "per_frame_item_count": len(per_frame_seq),
                "message": (
                    f"NumberOfFrames 声明 {declared} 帧，但 "
                    f"PerFrameFunctionalGroupsSequence 含 "
                    f"{len(per_frame_seq)} 个条目，无法逐帧对应"
                ),
            })
            continue

        frame_kvps, sources = _extract_frame_kvps(ds, declared)
        missing = sum(1 for value in frame_kvps if value is None)
        instances.append({
            "sop_instance_uid": report.sop_instance_uid,
            "output_file": report.output_name,
            "number_of_frames": declared,
            "kvp_source": _instance_source(sources),
            "frame_kvps": frame_kvps,
            "missing_kvp_frames": missing,
        })

    if problems:
        raise KvpError(
            "SERIES_KVP_CONFLICT",
            409,
            "系列内存在帧数声明冲突、输出歧义或不可读文件，"
            "无法给出完整的 KVP 分布统计",
            extra={
                "series_instance_uid": series_uid,
                "conflict_count": len(problems),
            },
            problems=sorted(
                problems,
                key=lambda p: (p.get("output_file") or "",
                               p.get("sop_instance_uid") or ""),
            ),
        )

    # ---- 汇总分布（实例明细按匿名 SOP UID 排列） -------------------------- #
    instances.sort(key=lambda item: item["sop_instance_uid"])

    distribution: dict[float, int] = {}
    total_frames = 0
    missing_frames = 0
    for instance in instances:
        total_frames += instance["number_of_frames"]
        missing_frames += instance["missing_kvp_frames"]
        for value in instance["frame_kvps"]:
            if value is not None:
                distribution[value] = distribution.get(value, 0) + 1

    return {
        "series_instance_uid": series_uid,
        "instance_count": len(instances),
        "total_frames": total_frames,
        "missing_kvp_frames": missing_frames,
        "mixed_kvp": len(distribution) > 1,
        "voltage_distribution": [
            {"kvp": kvp, "frame_count": distribution[kvp]}
            for kvp in sorted(distribution)
        ],
        "instances": instances,
    }
