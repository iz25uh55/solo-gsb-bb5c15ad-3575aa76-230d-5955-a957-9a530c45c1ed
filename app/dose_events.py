"""匿名 X-Ray Radiation Dose SR 的 CT 照射事件剂量（CTDIvol / DLP）解析。

数据来源严格限定为“已完成任务 output 目录中的匿名 DICOM 文件”，
**不重新解压或处理原始 ZIP**，只纳入成功写出的实例。

解析规则：

* 按匿名 SOPInstanceUID 定位唯一成功输出；文件必须是 X-Ray Radiation Dose SR
  （SOPClassUID = 1.2.840.10008.5.1.4.1.1.88.67）且含 ContentSequence；
* 递归遍历 ContentSequence：ConceptNameCodeSequence 为
  (113812, DCM, "CT Irradiation Event Data") 的容器划定一个 CT 照射事件边界；
* 事件边界内（任意嵌套深度）的 (113829, DCM, "CTDIvol") 与
  (113838, DCM, "DLP") NUM 项归属**最近（内层）事件**；事件边界之外的
  同类数值（报告级累计值，如 CT Accumulated Dose Data 容器内的
  CT Dose Length Product Total）一律忽略，不进入事件也不参与合计；
* 每个事件必须恰好含一个 CTDIvol（单位 mGy）与一个 DLP（单位 mGy·cm）。

存在以下任一情况时抛出 409（DOSE_SR_CONFLICT），在 problems[] 中一次性
给出**含匿名 SOP UID、事件序号与原因**的全部问题，不输出部分事件或部分合计：

* 事件内缺少 CTDIvol / DLP（``missing_value``）；
* 事件内同一量出现多次（``duplicate_value``）；
* 数值缺失或无法解析为有限数值（``invalid_value``）；
* 单位不符（CTDIvol 须为 mGy、DLP 须为 mGy.cm）（``unit_mismatch``）；
* 匿名输出不可读（``unreadable``）、不是 X-Ray Radiation Dose SR /
  缺少 ContentSequence（``not_dose_sr``），或文件内 SOPInstanceUID 与
  请求的匿名 UID 不一致（``sop_instance_uid_mismatch``）。
"""
from __future__ import annotations

import math
import os
from typing import Optional

import pydicom
from pydicom.errors import InvalidDicomError

from .anonymizer import FileReport, JobResult

# X-Ray Radiation Dose SR SOP Class UID
DOSE_SR_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.88.67"

# DCM 编码（ConceptNameCodeSequence 的 CodeValue + CodingSchemeDesignator）
_SCHEME_DCM = "DCM"
CODE_EVENT_CONTAINER = ("113812", _SCHEME_DCM)  # CT Irradiation Event Data
CODE_CTDI_VOL = ("113829", _SCHEME_DCM)         # CTDIvol
CODE_DLP = ("113838", _SCHEME_DCM)              # DLP

# UCUM 单位（MeasurementUnitsCodeSequence 的 CodeValue）
UNIT_MGY = "mGy"
UNIT_MGY_CM = "mGy.cm"

# detail.problems[].reason 取值（机器可读）
REASON_UNREADABLE = "unreadable"
REASON_NOT_DOSE_SR = "not_dose_sr"
REASON_SOP_UID_MISMATCH = "sop_instance_uid_mismatch"
REASON_MISSING_VALUE = "missing_value"
REASON_DUPLICATE_VALUE = "duplicate_value"
REASON_INVALID_VALUE = "invalid_value"
REASON_UNIT_MISMATCH = "unit_mismatch"

# 顶层 detail.reason 取值（非 problems 场景）
REASON_AMBIGUOUS_OUTPUT = "ambiguous_output"
REASON_OUTPUT_MISSING = "output_missing"

# problems[].quantity 取值
QUANTITY_CTDI_VOL = "ctdi_vol"
QUANTITY_DLP = "dlp"


class DoseEventsError(Exception):
    """剂量事件解析失败；http_status 由路由层映射为 HTTPException。"""

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
        raise DoseEventsError(
            "INSTANCE_NOT_FOUND",
            404,
            "该匿名 SOPInstanceUID 在本任务的成功输出中不存在",
            sop_uid=sop_uid,
        )
    if len(matches) > 1:
        output_files = sorted(r.output_name for r in matches)
        raise DoseEventsError(
            "AMBIGUOUS_INSTANCE_OUTPUT",
            409,
            "同一匿名 SOPInstanceUID 对应多份输出文件，无法确定要解析的剂量 SR",
            sop_uid=sop_uid,
            reason=REASON_AMBIGUOUS_OUTPUT,
            extra={"output_files": output_files},
        )
    return matches[0]


# --------------------------------------------------------------------------- #
# ContentSequence 遍历
# --------------------------------------------------------------------------- #
def _concept_code(item) -> Optional[tuple[str, str]]:
    """内容项 ConceptNameCodeSequence 首项的 (CodeValue, CodingSchemeDesignator)。"""
    seq = getattr(item, "ConceptNameCodeSequence", None)
    if not seq:
        return None
    code_item = seq[0]
    value = str(getattr(code_item, "CodeValue", "") or "").strip()
    designator = str(getattr(code_item, "CodingSchemeDesignator", "") or "").strip()
    if not value:
        return None
    return value, designator


class _EventAccumulator:
    """单个照射事件边界内收集到的 CTDIvol / DLP 内容项（遍历后统一校验）。"""

    __slots__ = ("index", "ctdi_vol_items", "dlp_items")

    def __init__(self, index: int) -> None:
        self.index = index
        self.ctdi_vol_items: list = []
        self.dlp_items: list = []


def _push_children(container, current_event: Optional[_EventAccumulator],
                   stack: list) -> None:
    """把容器的内容项逆序入栈，使弹出顺序即文档顺序。"""
    seq = getattr(container, "ContentSequence", None)
    if not seq:
        return
    for child in reversed(list(seq)):
        stack.append((child, current_event))


def _collect_events(root) -> list[_EventAccumulator]:
    """按内容顺序遍历 ContentSequence，返回全部 CT 照射事件及其数值内容项。

    显式栈避免深层嵌套触达递归上限；事件容器可嵌套（非标准但防御性处理）：
    内层容器成为新边界，其后的兄弟节点仍归属外层事件。
    """
    events: list[_EventAccumulator] = []
    stack: list[tuple[object, Optional[_EventAccumulator]]] = []
    _push_children(root, None, stack)
    while stack:
        item, current_event = stack.pop()
        code = _concept_code(item)
        if code == CODE_EVENT_CONTAINER:
            event = _EventAccumulator(len(events))
            events.append(event)
            _push_children(item, event, stack)
        elif code in (CODE_CTDI_VOL, CODE_DLP):
            # NUM 为叶子节点；仅归属最近（内层）事件，事件外的
            # 报告级累计值一律忽略
            if current_event is not None:
                if code == CODE_CTDI_VOL:
                    current_event.ctdi_vol_items.append(item)
                else:
                    current_event.dlp_items.append(item)
        else:
            _push_children(item, current_event, stack)
    return events


# --------------------------------------------------------------------------- #
# 数值解析与校验
# --------------------------------------------------------------------------- #
def _parse_measurement(item) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """解析 NUM 内容项，返回 (有限 float 或 None, 单位 CodeValue 或 None, 数值原文)。"""
    mvs = getattr(item, "MeasuredValueSequence", None)
    if not mvs or len(mvs) != 1:
        return None, None, None
    mv = mvs[0]
    raw = getattr(mv, "NumericValue", None)
    raw_text = None if raw is None else str(raw).strip()
    value: Optional[float] = None
    if raw_text:
        try:
            parsed = float(raw_text)
        except (TypeError, ValueError, OverflowError):
            parsed = math.nan
        if math.isfinite(parsed):
            value = parsed
    units_seq = getattr(mv, "MeasurementUnitsCodeSequence", None)
    units = None
    if units_seq:
        units = str(getattr(units_seq[0], "CodeValue", "") or "").strip() or None
    return value, units, raw_text


def _validate_quantity(
    event: _EventAccumulator,
    quantity: str,
    items: list,
    expected_units: str,
    problems: list[dict],
    sop_uid: str,
    output_file: str,
) -> Optional[float]:
    """校验事件内某个量的全部内容项；合法返回有限 float，否则记录问题返回 None。"""
    label = "CTDIvol" if quantity == QUANTITY_CTDI_VOL else "DLP"
    base = {
        "sop_instance_uid": sop_uid,
        "output_file": output_file,
        "event_index": event.index,
        "quantity": quantity,
    }
    if not items:
        problems.append({
            **base,
            "reason": REASON_MISSING_VALUE,
            "message": f"事件 {event.index} 缺少 {label} 数值项",
        })
        return None
    if len(items) > 1:
        problems.append({
            **base,
            "reason": REASON_DUPLICATE_VALUE,
            "message": (
                f"事件 {event.index} 内 {label} 出现 {len(items)} 次，"
                "无法确定唯一取值"
            ),
        })
        return None

    value, units, raw_text = _parse_measurement(items[0])
    valid = True
    if value is None:
        problem = {
            **base,
            "reason": REASON_INVALID_VALUE,
            "message": (
                f"事件 {event.index} 的 {label} 数值缺失或无法解析为有限数值"
            ),
        }
        if raw_text:
            problem["numeric_value"] = raw_text
            problem["message"] += f"：{raw_text!r}"
        problems.append(problem)
        valid = False
    if units != expected_units:
        problems.append({
            **base,
            "reason": REASON_UNIT_MISMATCH,
            "units": units,
            "message": (
                f"事件 {event.index} 的 {label} 单位须为 {expected_units}，"
                f"实际为 {units or '<缺失>'}"
            ),
        })
        valid = False
    return value if valid else None


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def extract_dose_events(job, sop_uid: str) -> dict:
    """按匿名 SOP UID 定位任务成功输出并解析 CT 照射事件剂量。

    任何定位/解析/校验错误均抛 DoseEventsError；成功时返回按内容顺序排列的
    事件剂量与事件 DLP 之和（报告级累计值不参与合计）。
    """
    result: JobResult = job.result
    report = _locate_unique_output(result, sop_uid)
    path = os.path.join(result.output_dir, report.output_name)
    if not os.path.isfile(path):
        raise DoseEventsError(
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
        raise DoseEventsError(
            "DOSE_SR_CONFLICT",
            409,
            "匿名输出不是可读取的 DICOM 文件，无法解析剂量 SR",
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
        raise DoseEventsError(
            "DOSE_SR_CONFLICT",
            409,
            "读取匿名输出失败，无法解析剂量 SR",
            sop_uid=sop_uid,
            extra={"conflict_count": 1},
            problems=[{
                "sop_instance_uid": sop_uid,
                "output_file": report.output_name,
                "reason": REASON_UNREADABLE,
                "message": f"读取匿名输出失败：{exc}",
            }],
        ) from exc

    problems: list[dict] = []

    actual_sop = str(getattr(ds, "SOPInstanceUID", "") or "").strip()
    if actual_sop and actual_sop != sop_uid:
        problems.append({
            "sop_instance_uid": sop_uid,
            "output_file": report.output_name,
            "reason": REASON_SOP_UID_MISMATCH,
            "message": (
                "文件内 SOPInstanceUID 与请求的匿名 SOP UID 不一致，拒绝解析："
                f"文件内为 {actual_sop}，请求为 {sop_uid}"
            ),
        })

    sop_class = str(getattr(ds, "SOPClassUID", "") or "").strip()
    has_content = getattr(ds, "ContentSequence", None) is not None
    if sop_class != DOSE_SR_SOP_CLASS:
        problems.append({
            "sop_instance_uid": sop_uid,
            "output_file": report.output_name,
            "reason": REASON_NOT_DOSE_SR,
            "message": (
                "该实例不是 X-Ray Radiation Dose SR（SOPClassUID="
                f"{sop_class or '<缺失>'}），无法解析 CT 照射事件"
            ),
        })
    elif not has_content:
        problems.append({
            "sop_instance_uid": sop_uid,
            "output_file": report.output_name,
            "reason": REASON_NOT_DOSE_SR,
            "message": "剂量 SR 缺少 ContentSequence，无法解析 CT 照射事件",
        })

    events: list[_EventAccumulator] = []
    values: list[tuple[Optional[float], Optional[float]]] = []
    if not problems:
        events = _collect_events(ds)
        for event in events:
            ctdi_vol = _validate_quantity(
                event, QUANTITY_CTDI_VOL, event.ctdi_vol_items, UNIT_MGY,
                problems, sop_uid, report.output_name,
            )
            dlp = _validate_quantity(
                event, QUANTITY_DLP, event.dlp_items, UNIT_MGY_CM,
                problems, sop_uid, report.output_name,
            )
            values.append((ctdi_vol, dlp))

    if problems:
        raise DoseEventsError(
            "DOSE_SR_CONFLICT",
            409,
            "剂量 SR 存在缺项、重复项、非有限数值或单位不符，"
            "无法给出完整的 CT 照射事件剂量（不输出部分合计）",
            sop_uid=sop_uid,
            extra={"conflict_count": len(problems)},
            problems=problems,
        )

    # problems 为空时所有数值均已校验为有限 float
    out_events = [
        {"index": event.index, "ctdi_vol_mgy": ctdi_vol, "dlp_mgy_cm": dlp}
        for event, (ctdi_vol, dlp) in zip(events, values)
    ]
    return {
        "sop_instance_uid": sop_uid,
        "output_file": report.output_name,
        "event_count": len(out_events),
        "total_dlp_mgy_cm": math.fsum(dlp for _, dlp in values),
        "events": out_events,
    }
