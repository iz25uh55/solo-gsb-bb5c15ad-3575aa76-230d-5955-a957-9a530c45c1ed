"""系列切片几何分析：方向法线投影排序与层间距缺口检查。

数据来源严格限定为“已完成任务 output 目录中的匿名 DICOM 文件”，
**不重新解压或处理原始 ZIP**，并且只纳入**成功写出的单帧实例**
（`NumberOfFrames` 缺省或为 1；多帧实例不参与几何分析，单独在
`excluded_instances` 中说明）。

分析流程：

1. 按匿名 SeriesInstanceUID 选出该系列的全部成功输出，逐个读取
   `ImageOrientationPatient`（行/列方向余弦）与
   `ImagePositionPatient`（片层左上中心坐标）；
2. 以 ``行方向 × 列方向`` 叉积得到单位法线，把各实例位置投影到法线；
3. 存在以下任一情况时返回 ``409 SERIES_GEOMETRY_CONFLICT``，并在
   ``problems[]`` 中一次性给出**含匿名 SOP UID** 的全部冲突明细：

   * 某实例几何标签缺失 / 无法解析 / 取值非法（`invalid_geometry`）；
   * 实例的 `FrameOfReferenceUID` 不一致（`frame_of_reference_mismatch`）；
   * `ImageOrientationPatient` 方向余弦不一致（`orientation_mismatch`，
     含正反方向相反的情况）；
   * 两个实例在法线上的投影位置重合（`duplicate_projection`）；

4. 几何一致时按投影坐标升序排列，以相邻间距的**中位数**为基准，
   把大于 ``1.5 × 中位数`` 的相邻间隙标记为异常缺口；
   有效单帧实例不足 3 张（间距不足 2 个）时无法判断缺口，
   返回 ``gap_check="insufficient_slices"``。
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError

from .anonymizer import FileReport, JobResult

# 方向余弦逐分量一致性容差（正交化后的单位分量误差）
ORIENTATION_TOLERANCE = 1e-4
# 投影位置重合判定容差（毫米）：|p1 - p2| <= 该值即视为重合
POSITION_TOLERANCE = 1e-6
# 缺口倍数阈值：间距 > 1.5 × 中位数间距 即异常
GAP_FACTOR = 1.5
# 方向余弦分量的合法闭区间（单位向量分量必须在 [-1, 1]）
_COS_LIMIT = 1.0 + 1e-6

# detail.problems[].reason 取值（机器可读）
REASON_UNREADABLE = "unreadable"
REASON_OUTPUT_MISSING = "output_missing"
REASON_AMBIGUOUS_OUTPUT = "ambiguous_output"
REASON_INVALID_GEOMETRY = "invalid_geometry"
REASON_FRAME_MISMATCH = "frame_of_reference_mismatch"
REASON_ORIENTATION_MISMATCH = "orientation_mismatch"
REASON_DUPLICATE_PROJECTION = "duplicate_projection"
REASON_MULTIFRAME = "multiframe"

_GEOMETRY_TAGS = ("FrameOfReferenceUID", "ImageOrientationPatient",
                  "ImagePositionPatient")


class GeometryError(Exception):
    """系列几何分析失败；http_status 由路由层映射为 HTTPException。"""

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


@dataclass
class _Slice:
    """通过几何校验的单帧实例。"""
    report: FileReport
    sop_uid: str
    frame_uid: str
    row_cosines: np.ndarray  # ImageOrientationPatient 前 3 个分量
    col_cosines: np.ndarray  # ImageOrientationPatient 后 3 个分量
    normal: np.ndarray       # 单位法线（row × col）
    position: np.ndarray     # ImagePositionPatient（3 分量，毫米）
    projection: float       # 位置在法线上的标量投影


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
                    "无法确定用于几何分析的唯一实例"
                ),
            })
    return problems


# --------------------------------------------------------------------------- #
# 标签解析
# --------------------------------------------------------------------------- #
def _parse_components(value: object, count: int, sop_uid: str,
                      keyword: str) -> list[float]:
    """解析恰含 ``count`` 个有限数值分量的 DS/FD 多值元素；任何异常都转 ValueError。"""
    if value is None:
        raise ValueError(f"{keyword} 缺失")
    if isinstance(value, pydicom.multival.MultiValue):
        raw = list(value)
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        # VM=1 或反斜杠字符串
        text = str(value).strip()
        raw = text.split("\\") if "\\" in text else [text]
    if len(raw) != count:
        raise ValueError(
            f"{keyword} 必须恰好包含 {count} 个分量，收到 {len(raw)} 个"
        )
    components: list[float] = []
    for part in raw:
        text = str(part).strip()
        if not text:
            raise ValueError(f"{keyword} 包含空分量")
        components.append(float(text))
    if not all(math.isfinite(v) for v in components):
        raise ValueError(f"{keyword} 包含非有限数值（NaN/Inf）")
    return components


def _frame_uid(ds) -> str:
    uid = str(getattr(ds, "FrameOfReferenceUID", "") or "").strip()
    if not uid:
        raise ValueError("FrameOfReferenceUID 缺失或为空")
    return uid


def _is_single_frame(ds) -> tuple[bool, Optional[str]]:
    """判断是否单帧实例。

    返回 (是否单帧, 原始 NumberOfFrames 文本)；缺省/为 1 视为单帧，
    显式 >1 为多帧；取值非法也按多帧排除（无法确认帧数）。
    """
    raw = getattr(ds, "NumberOfFrames", None)
    if raw is None or not str(raw).strip():
        return True, None
    try:
        frames = int(str(raw).strip())
    except (TypeError, ValueError):
        return False, str(raw)
    return (frames == 1, str(raw)) if frames > 0 else (False, str(raw))


def _build_slice(report: FileReport, ds) -> _Slice:
    """读取并校验几何标签，构造法线/投影所需的中间结构。"""
    sop_uid = str(getattr(ds, "SOPInstanceUID", "") or "").strip() \
        or report.sop_instance_uid

    frame_uid = _frame_uid(ds)

    iop = _parse_components(
        getattr(ds, "ImageOrientationPatient", None), 6,
        sop_uid, "ImageOrientationPatient",
    )
    ipp = _parse_components(
        getattr(ds, "ImagePositionPatient", None), 3,
        sop_uid, "ImagePositionPatient",
    )

    row = np.asarray(iop[:3], dtype=np.float64)
    col = np.asarray(iop[3:], dtype=np.float64)
    if any(abs(v) > _COS_LIMIT for v in row) or any(abs(v) > _COS_LIMIT for v in col):
        raise ValueError(
            "ImageOrientationPatient 方向余弦分量超出 [-1, 1] 合法范围"
        )
    row_norm = float(np.linalg.norm(row))
    col_norm = float(np.linalg.norm(col))
    if row_norm <= ORIENTATION_TOLERANCE or col_norm <= ORIENTATION_TOLERANCE:
        raise ValueError(
            "ImageOrientationPatient 行/列方向向量长度为 0，无法构成平面"
        )

    row_u = row / row_norm
    col_u = col / col_norm
    # 行、列方向理论上正交；点积明显偏离 0 说明标签不自洽
    if abs(float(np.dot(row_u, col_u))) > 1e-3:
        raise ValueError(
            "ImageOrientationPatient 行/列方向余弦不正交，平面方向不自洽"
        )

    normal = np.cross(row_u, col_u)
    n_norm = float(np.linalg.norm(normal))
    if n_norm <= ORIENTATION_TOLERANCE:
        raise ValueError("行/列方向叉积得到零法线，平面方向退化")
    normal = normal / n_norm

    position = np.asarray(ipp, dtype=np.float64)
    projection = float(np.dot(position, normal))

    return _Slice(
        report=report,
        sop_uid=sop_uid,
        frame_uid=frame_uid,
        row_cosines=row_u,
        col_cosines=col_u,
        normal=normal,
        position=position,
        projection=projection,
    )


# --------------------------------------------------------------------------- #
# 冲突分组
# --------------------------------------------------------------------------- #
def _orientation_key(slice_: _Slice) -> tuple:
    return tuple(np.round(np.concatenate(
        (slice_.row_cosines, slice_.col_cosines)
    ) / ORIENTATION_TOLERANCE).astype(int).tolist())


def _frame_problems(slices: list[_Slice]) -> list[dict]:
    """FrameOfReferenceUID 不一致：按 UID 分组，每组给出成员匿名 SOP UID。"""
    groups: dict[str, list[str]] = {}
    for slice_ in slices:
        groups.setdefault(slice_.frame_uid, []).append(slice_.sop_uid)
    if len(groups) <= 1:
        return []

    # 参照组固定取**系列内第一个被处理的实例**（按输出文件名排序）所属组，
    # 保证几何基准与输入文件顺序一致、可复现
    reference = slices[0].frame_uid
    problems: list[dict] = []
    for frame_uid in sorted(uid for uid in groups if uid != reference):
        members = sorted(groups[frame_uid])
        problems.append({
            "sop_instance_uid": members[0],
            "sop_instance_uids": members,
            "reason": REASON_FRAME_MISMATCH,
            "frame_of_reference_uid": frame_uid,
            "expected_frame_of_reference_uid": reference,
            "message": (
                f"FrameOfReferenceUID 与系列基准（{reference}）不一致："
                f"{frame_uid}（{len(members)} 个实例）"
            ),
        })
    return problems


def _orientation_problems(slices: list[_Slice]) -> list[dict]:
    """ImageOrientationPatient 不一致（含反向）：按方向键分组报冲突。"""
    groups: dict[tuple, list[_Slice]] = {}
    for slice_ in slices:
        groups.setdefault(_orientation_key(slice_), []).append(slice_)
    if len(groups) <= 1:
        return []

    ordered = sorted(
        groups,
        key=lambda key: (-len(groups[key]), tuple(key)),
    )
    # 参照组固定取**系列内第一个被处理的实例**（按输出文件名排序）所属组，
    # 保证几何基准与输入文件顺序一致、可复现
    ref_key = _orientation_key(slices[0])
    ref_slices = groups[ref_key]
    ref_row = ref_slices[0].row_cosines
    ref_col = ref_slices[0].col_cosines
    ref_normal = ref_slices[0].normal

    def _dot_normal(first: _Slice) -> float:
        return float(np.dot(first.normal, ref_normal))

    problems: list[dict] = []
    for key in ordered:
        if key == ref_key:
            continue
        members_slices = sorted(groups[key], key=lambda s: s.sop_uid)
        members = [s.sop_uid for s in members_slices]
        first = members_slices[0]
        # 法线反向（点积 ≈ -1）即从相反一侧观察，投影排序方向相反
        opposite = _dot_normal(first) < -(1.0 - 1e-3)
        problems.append({
            "sop_instance_uid": members[0],
            "sop_instance_uids": members,
            "reason": REASON_ORIENTATION_MISMATCH,
            "opposite_direction": opposite,
            "image_orientation_patient": [
                float(v) for v in np.concatenate(
                    (first.row_cosines, first.col_cosines)
                )
            ],
            "expected_image_orientation_patient": [
                float(v) for v in np.concatenate((ref_row, ref_col))
            ],
            "normal": [float(v) for v in first.normal],
            "expected_normal": [float(v) for v in ref_normal],
            "message": (
                "ImageOrientationPatient 与系列基准方向相反"
                if opposite
                else "ImageOrientationPatient 与系列基准方向不一致"
            )
            + f"（{len(members)} 个实例）",
        })
    return problems


def _duplicate_projection_problems(slices: list[_Slice]) -> list[dict]:
    """投影位置重合：按投影聚类，同簇成员两两属于同一冲突明细。"""
    # 先按投影排序，相邻差 <= 容差即同簇（容差很小，传递闭包足够稳健）
    ordered = sorted(slices, key=lambda s: (s.projection, s.sop_uid))
    clusters: list[list[_Slice]] = []
    for slice_ in ordered:
        if clusters and abs(slice_.projection - clusters[-1][-1].projection) \
                <= POSITION_TOLERANCE:
            clusters[-1].append(slice_)
        else:
            clusters.append([slice_])

    problems: list[dict] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        members = sorted(s.sop_uid for s in cluster)
        positions = sorted(
            ({"sop_instance_uid": s.sop_uid, "projection": s.projection}
             for s in cluster),
            key=lambda item: item["sop_instance_uid"],
        )
        problems.append({
            "sop_instance_uid": members[0],
            "sop_instance_uids": members,
            "reason": REASON_DUPLICATE_PROJECTION,
            "projection": cluster[0].projection,
            "positions": positions,
            "message": (
                f"{len(members)} 个实例在方向法线上的投影位置重合"
                f"（投影≈{cluster[0].projection:.6g} mm），无法唯一排序"
            ),
        })
    return problems


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def analyze_series_geometry(job, series_uid: str) -> dict:
    """读取系列成功输出并做几何分析；任何错误均抛 GeometryError。"""
    result: JobResult = job.result
    reports = _series_reports(result, series_uid)
    if not reports:
        raise GeometryError(
            "SERIES_NOT_FOUND",
            404,
            "该匿名 SeriesInstanceUID 在本任务的成功输出中不存在",
            extra={"series_instance_uid": series_uid},
        )

    problems: list[dict] = _detect_ambiguous_outputs(reports)
    ambiguous_sops = {p["sop_instance_uid"] for p in problems}

    slices: list[_Slice] = []
    excluded: list[dict] = []

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

        is_single, frames_text = _is_single_frame(ds)
        if not is_single:
            excluded.append({
                "sop_instance_uid": report.sop_instance_uid,
                "output_file": report.output_name,
                "reason": REASON_MULTIFRAME,
                "number_of_frames": frames_text,
                "message": (
                    f"NumberOfFrames={frames_text} 的多帧实例不参与单帧切片几何分析"
                ),
            })
            continue

        try:
            slice_ = _build_slice(report, ds)
        except (ValueError, TypeError, OverflowError) as exc:
            problems.append({
                "sop_instance_uid": report.sop_instance_uid,
                "output_file": report.output_name,
                "reason": REASON_INVALID_GEOMETRY,
                "missing_or_invalid_tags": [
                    keyword
                    for keyword in _GEOMETRY_TAGS
                    if keyword not in ds or not str(
                        getattr(ds, keyword, "") or ""
                    ).strip()
                ],
                "message": f"几何标签无效：{exc}",
            })
            continue

        slices.append(slice_)

    # 几何冲突：FrameOfReference / 方向 / 投影重合（投影重合仅在前两者一致时有意义）
    frame_problems = _frame_problems(slices)
    orientation_problems = _orientation_problems(slices)
    problems.extend(frame_problems)
    problems.extend(orientation_problems)
    if not frame_problems and not orientation_problems:
        problems.extend(_duplicate_projection_problems(slices))

    if problems:
        raise GeometryError(
            "SERIES_GEOMETRY_CONFLICT",
            409,
            "系列几何不一致或存在无效几何标签，无法进行可靠的切片排序与缺口分析",
            extra={
                "series_instance_uid": series_uid,
                "single_frame_instance_count": len(slices),
                "conflict_count": len(problems),
            },
            problems=sorted(
                problems,
                key=lambda p: (p.get("output_file") or "",
                               p.get("sop_instance_uid") or ""),
            ),
        )

    if not slices:
        # 系列有成功输出，但全部是多帧实例（无任何可分析的单帧实例）
        raise GeometryError(
            "NO_SINGLE_FRAME_INSTANCES",
            409,
            "该系列的成功输出中没有可用于几何分析的单帧实例（全部为多帧实例）",
            extra={
                "series_instance_uid": series_uid,
                "single_frame_instance_count": 0,
            },
            problems=sorted(
                excluded, key=lambda item: item["output_file"]
            ),
        )

    # ---- 几何一致：投影排序 + 中位数层间距缺口 ---------------------------- #
    ordered = sorted(slices, key=lambda s: (s.projection, s.sop_uid))
    reference = ordered[0]
    projections = [s.projection for s in ordered]
    spacings = [
        projections[i] - projections[i - 1] for i in range(1, len(ordered))
    ]

    gap_check = "ok"
    median_spacing: Optional[float] = None
    anomalous_indices: list[int] = []
    if len(ordered) < 3:
        gap_check = "insufficient_slices"
    else:
        median_spacing = float(np.median(np.asarray(spacings, dtype=np.float64)))
        if median_spacing <= POSITION_TOLERANCE:
            # 理论上重合已在上面拦截；中位数为 0 属于退化几何，按冲突处理
            raise GeometryError(
                "SERIES_GEOMETRY_CONFLICT",
                409,
                "相邻投影距离中位数为 0，系列层间距退化，无法判断缺口",
                extra={"series_instance_uid": series_uid,
                       "single_frame_instance_count": len(ordered)},
            )
        anomalous_indices = [
            index
            for index, gap in enumerate(spacings)
            if gap > GAP_FACTOR * median_spacing
        ]

    spacing_items: list[dict] = []
    for index, gap in enumerate(spacings):
        spacing_items.append({
            "index": index,
            "from_sop_instance_uid": ordered[index].sop_uid,
            "to_sop_instance_uid": ordered[index + 1].sop_uid,
            "distance": gap,
            "anomalous": index in anomalous_indices,
        })

    return {
        "series_instance_uid": series_uid,
        "frame_of_reference_uid": reference.frame_uid,
        "image_orientation_patient": [
            float(v) for v in np.concatenate(
                (reference.row_cosines, reference.col_cosines)
            )
        ],
        "normal": [float(v) for v in reference.normal],
        "instance_count": len(ordered),
        "gap_check": gap_check,
        "median_spacing": median_spacing,
        "gap_factor": GAP_FACTOR,
        "anomalous_gap_indices": anomalous_indices,
        "slices": [
            {
                "index": index,
                "sop_instance_uid": slice_.sop_uid,
                "projection": slice_.projection,
                "image_position_patient": [float(v) for v in slice_.position],
            }
            for index, slice_ in enumerate(ordered)
        ],
        "spacings": spacing_items,
        "message": (
            None
            if len(ordered) >= 3
            else "有效单帧实例不足 3 张（相邻间距不足 2 个），无法判断异常间隙"
        ),
        "excluded_instances": sorted(
            excluded, key=lambda item: item["output_file"]
        ),
    }
