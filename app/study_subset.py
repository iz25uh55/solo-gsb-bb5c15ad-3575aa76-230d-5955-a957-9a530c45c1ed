"""按匿名 StudyInstanceUID 下载检查子集（引用闭包自动补全）。

数据来源严格限定为“已完成任务的成功输出记录及 output_dir 中的匿名 DICOM 文件”，
**不重新解压或处理原始 ZIP**：

1. 以匿名 StudyInstanceUID 对应的全部成功输出**记录**作为初选集合（不预先按
   磁盘文件存在性过滤，避免丢片被静默剔除后仍产出残缺 ZIP）；
2. 递归读取集合内匿名 DICOM 任意序列层级的 ReferencedSOPInstanceUID，
   把同任务内被引用的实例（可能属于其他检查）补入集合，循环至闭包稳定；
3. 对初选及递归补入的每个实例逐一核对物理文件：任务记录有成功输出但
   output_dir 中文件已丢失时，与“引用目标不在成功输出中”“一个匿名 SOP UID
   对应多个输出文件”一并汇总，一律拒绝输出（路由层返回 409 与具体匿名
   UID/原因/output_files），不产生残缺 ZIP。
"""
from __future__ import annotations

import os
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Optional

import pydicom
from pydicom.sequence import Sequence

from .anonymizer import FileReport, JobResult

REFERENCED_SOP_KEYWORD = "ReferencedSOPInstanceUID"

# 409 问题原因
REASON_NOT_IN_OUTPUT = "not_in_output"
REASON_AMBIGUOUS_OUTPUT = "ambiguous_output"
REASON_OUTPUT_MISSING = "output_missing"

# 同一 UID 上多类问题并存时的原因优先级（数值越小越优先）
_REASON_PRIORITY = {
    REASON_OUTPUT_MISSING: 0,
    REASON_AMBIGUOUS_OUTPUT: 1,
    REASON_NOT_IN_OUTPUT: 2,
}
_PROBLEM_MESSAGES = {
    REASON_NOT_IN_OUTPUT: "引用目标不在本任务的成功输出中",
    REASON_AMBIGUOUS_OUTPUT: "一个匿名 SOPInstanceUID 对应多个输出文件，无法唯一补入",
    REASON_OUTPUT_MISSING: "任务成功输出记录存在，但 output_dir 中的物理文件已丢失",
}


class StudySubsetError(Exception):
    """子集解析失败；http_status 由路由层映射为 HTTPException。"""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        study_uid: Optional[str] = None,
        problems: Optional[list[dict]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.detail: dict = {"code": code, "message": message}
        if study_uid is not None:
            self.detail["study_instance_uid"] = study_uid
        if problems:
            self.detail["problems"] = problems


@dataclass
class StudyClosure:
    initial_reports: list[FileReport]
    included_reports: list[FileReport]

    @property
    def initial_count(self) -> int:
        return len(self.initial_reports)

    @property
    def added_count(self) -> int:
        initial_names = {r.output_name for r in self.initial_reports}
        return sum(r.output_name not in initial_names for r in self.included_reports)

    @property
    def included_output_names(self) -> list[str]:
        return sorted(r.output_name for r in self.included_reports)


# --------------------------------------------------------------------------- #
# 索引与引用遍历
# --------------------------------------------------------------------------- #
def _output_path(result: JobResult, report: FileReport) -> str:
    return os.path.join(result.output_dir, report.output_name)


def _successful_outputs(result: JobResult) -> list[FileReport]:
    """任务内真正写出的匿名输出**记录**（error/无输出名/无匿名 SOP UID 者剔除）。

    注意：这里**不**按磁盘文件存在性过滤——任务成功输出记录是初选与引用索引的
    唯一依据；物理文件是否仍在 output_dir 中，交由闭包解析阶段逐个核对，
    使“记录存在但文件丢失”显式表现为 409，而不是静默产出缺片 ZIP。
    """
    return [
        report
        for report in result.files
        if report.status != "error"
        and report.output_name
        and report.sop_instance_uid
    ]


def _build_sop_index(result: JobResult) -> dict[str, list[FileReport]]:
    """匿名 SOPInstanceUID -> 成功输出记录（通常 1 个；重复 SOP 时多个）。

    索引基于任务成功输出记录建立，与 output_dir 的当前物理状态无关。
    """
    index: dict[str, list[FileReport]] = {}
    for report in _successful_outputs(result):
        index.setdefault(report.sop_instance_uid, []).append(report)
    return index


def _split_ui_values(value: object):
    """展开 ReferencedSOPInstanceUID 的单值 / VM>1（反斜杠分隔）/ 多值形式。"""
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = str(value).split("\\")
    for part in parts:
        text = str(part).strip()
        if text:
            yield text


def iter_referenced_sop_uids(ds) -> list[str]:
    """收集匿名 DICOM 中任意序列层级的全部 ReferencedSOPInstanceUID。"""
    refs: list[str] = []
    stack = [ds]
    while stack:
        current = stack.pop()
        for tag_ in list(current.keys()):
            elem = current[tag_]
            if elem.VR == "SQ" and isinstance(elem.value, Sequence):
                stack.extend(elem.value)
                continue
            if (getattr(elem, "keyword", "") or "") == REFERENCED_SOP_KEYWORD:
                refs.extend(_split_ui_values(elem.value))
    return refs


# --------------------------------------------------------------------------- #
# 引用闭包
# --------------------------------------------------------------------------- #
def resolve_study_closure(result: JobResult, study_uid: str) -> StudyClosure:
    """返回某匿名检查在本任务成功输出记录上的引用闭包；无法闭合时抛 StudySubsetError。

    初选与引用索引均以任务成功输出**记录**为准；对初选及递归补入的每条记录
    都会核对 output_dir 中的物理文件。任何实例缺失、引用未收录或重复 SOP 歧义
    都在同一次遍历中汇总，最终一次性以 409 报出，不生成残缺 ZIP。
    """
    index = _build_sop_index(result)

    initial = [
        report
        for report in _successful_outputs(result)
        if report.study_instance_uid == study_uid
    ]
    if not initial:
        raise StudySubsetError(
            "STUDY_NOT_FOUND",
            404,
            "该匿名 StudyInstanceUID 在本任务的成功输出中不存在",
            study_uid=study_uid,
        )

    chosen: dict[str, FileReport] = {r.output_name: r for r in initial}
    queue: list[FileReport] = list(initial)
    queued = {r.output_name for r in initial}
    # uid -> (reason, output_names)；同一 UID 只报一次，按“缺片优先”保留最强判定
    problems: dict[str, tuple[str, set[str]]] = {}

    def note_problem(uid: str, reason: str, output_names: list[str]) -> None:
        """记录某匿名 SOP UID 上的问题；原因按优先级合并，output_files 取并集。"""
        if uid in problems:
            old_reason, old_files = problems[uid]
            if _REASON_PRIORITY[reason] >= _REASON_PRIORITY[old_reason]:
                old_files.update(output_names)
                return
            files = set(output_names) | old_files
        else:
            files = set(output_names)
        problems[uid] = (reason, files)

    def register(uid: str, targets: list[FileReport]) -> None:
        """登记某 SOP UID 的全部输出记录：核对物理文件、标记歧义、并入遍历队列。"""
        if not targets:
            note_problem(uid, REASON_NOT_IN_OUTPUT, [])
            return
        missing = [
            t.output_name
            for t in targets
            if not os.path.isfile(_output_path(result, t))
        ]
        if missing:
            note_problem(uid, REASON_OUTPUT_MISSING, missing)
        elif len(targets) > 1:
            # 仅当物理文件齐全时“歧义”才是该 UID 的最终定性：
            # 缺片与歧义并存时按缺片上报（不挑选、不猜测）
            note_problem(
                uid,
                REASON_AMBIGUOUS_OUTPUT,
                sorted(t.output_name for t in targets),
            )
        # 即使存在问题也继续向下扫描，尽量一次性暴露全部闭包问题；
        # 有 problems 时最终必定 409，chosen 仅用于遍历，不会被输出。
        for target in targets:
            chosen.setdefault(target.output_name, target)
            if target.output_name not in queued:
                queued.add(target.output_name)
                queue.append(target)

    while queue:
        report = queue.pop()
        # 闭包成员自身的匿名 SOP UID 若缺片/对应多个输出文件，同样登记：
        # 不挑选、不猜测，直接进入 409（全量下载行为不受影响）
        register(report.sop_instance_uid, index.get(report.sop_instance_uid, []))

        # 记录在、物理文件没了：无法读取其引用，跳过扫描（缺失已在 register 中登记）
        if not os.path.isfile(_output_path(result, report)):
            continue
        try:
            ds = pydicom.dcmread(_output_path(result, report))
        except Exception as exc:  # 自己写出的匿名文件不可读属于服务端异常
            raise StudySubsetError(
                "ANONYMIZED_OUTPUT_UNREADABLE",
                500,
                f"无法读取匿名输出文件 {report.output_name}：{exc}",
                study_uid=study_uid,
            ) from exc

        for ref_uid in iter_referenced_sop_uids(ds):
            register(ref_uid, index.get(ref_uid, []))

    if problems:
        problem_items = [
            {
                "sop_instance_uid": uid,
                "reason": reason,
                "output_files": sorted(output_files),
                "message": _PROBLEM_MESSAGES[reason],
            }
            for uid, (reason, output_files) in sorted(problems.items())
        ]
        raise StudySubsetError(
            "REFERENCE_CLOSURE_INCOMPLETE",
            409,
            "引用闭包无法在本任务的成功输出内闭合，已拒绝输出子集 ZIP",
            study_uid=study_uid,
            problems=problem_items,
        )

    return StudyClosure(
        initial_reports=initial,
        included_reports=[chosen[name] for name in sorted(chosen)],
    )


# --------------------------------------------------------------------------- #
# 扁平 ZIP（复用匿名输出文件，不再触碰原始 ZIP / 解压目录）
# --------------------------------------------------------------------------- #
def build_subset_zip(output_dir: str, output_names: list[str], dest_zip: str) -> str:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in output_names:  # 调用方已排序；arcname 扁平、即匿名文件名
            zf.write(os.path.join(output_dir, name), arcname=name)
    return dest_zip


def write_study_subset_zip(job, study_uid: str) -> tuple[str, int, int]:
    """构建子集 ZIP 到任务目录下的临时文件，返回 (路径, 初选数, 补入数)。"""
    result: JobResult = job.result
    closure = resolve_study_closure(result, study_uid)

    fd, tmp_path = tempfile.mkstemp(
        prefix=f"study-subset-{job.id[:8]}-",
        suffix=".zip",
        dir=os.path.dirname(result.output_dir),
    )
    os.close(fd)
    try:
        build_subset_zip(result.output_dir, closure.included_output_names, tmp_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path, closure.initial_count, closure.added_count
