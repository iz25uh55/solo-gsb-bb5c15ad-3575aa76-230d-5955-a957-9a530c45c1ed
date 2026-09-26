"""为已完成任务的匿名输出生成带 DICOMDIR 的离线阅片 ZIP。

数据来源严格限定为“已完成任务 output_dir 中的匿名 DICOM 文件”，
**不重新解压或处理原始 ZIP**，也不会把任何原始路径 / 身份字段带入包内：

1. 逐个读取任务的成功匿名输出，按文件实际标签重建
   患者（伪名）→ 检查 → 系列 → 实例四级层级；
2. 实例在 ZIP 内使用 pydicom FileSet 语义分配的稳定短 File ID
   （``PTxxxxxx/STxxxxxx/SExxxxxx/IMxxxxxx``，满足 DICOM File ID
   组件长度 / 字符集 / 层级数约束），同一任务的同一批输出按固定键排序
   加入，保证 File ID 可复现；
3. DICOMDIR 记录中的 Referenced File ID 必须逐一指向 ZIP 内实际写入的
   文件，写出后会重新加载校验；
4. 任一匿名输出不可读、缺少建目录必需标签，或同一匿名 SOPInstanceUID
   对应多份输出时，收集**全部**问题文件及原因后返回错误，
   **绝不下载残缺目录包**。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Optional

import pydicom
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError
from pydicom.fileset import FileSet, RecordNode

from .anonymizer import FileReport, JobResult

# 问题原因（写入 detail.problems[].reason）
REASON_UNREADABLE = "unreadable"
REASON_MISSING_TAGS = "missing_required_tags"
REASON_AMBIGUOUS_OUTPUT = "ambiguous_output"
REASON_OUTPUT_MISSING = "output_missing"

# 建 DICOMDIR 必须从匿名文件本身读到的标签：
# 患者伪名（PatientID/PatientName）、Study/Series/SOP 三级 UID、
# SOP Class UID 与文件元信息中的 Transfer Syntax UID（叶级目录记录引用必需），
# 以及 SERIES 记录必有的 Modality。其余目录描述字段（StudyDate/StudyID/
# SeriesNumber/InstanceNumber 等）为 Type 2，存在则带入，缺失不拒绝。
CORE_REQUIRED_KEYWORDS: tuple[str, ...] = (
    "PatientID",
    "PatientName",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "SOPClassUID",
    "Modality",
)

_TAG_LABELS = {
    "PatientID": "患者 ID（伪名）",
    "PatientName": "患者姓名（伪名）",
    "StudyInstanceUID": "Study Instance UID",
    "SeriesInstanceUID": "Series Instance UID",
    "SOPInstanceUID": "SOP Instance UID",
    "SOPClassUID": "SOP Class UID",
    "Modality": "模态",
    "TransferSyntaxUID": "传输语法 UID（文件元信息）",
}


class DicomDirError(Exception):
    """DICOMDIR 包构建失败；http_status 由路由层映射为 HTTPException。"""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        problems: Optional[list[dict]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.detail: dict = {"code": code, "message": message}
        if problems:
            self.detail["problems"] = problems


@dataclass
class DicomDirStats:
    file_count: int
    patient_count: int
    study_count: int
    series_count: int


# --------------------------------------------------------------------------- #
# 输出收集与逐文件校验
# --------------------------------------------------------------------------- #
def _output_path(result: JobResult, report: FileReport) -> str:
    return os.path.join(result.output_dir, report.output_name)


def _successful_reports(result: JobResult) -> list[FileReport]:
    """任务内真正写出匿名文件的报告（error/无输出名者剔除）。"""
    return [
        report
        for report in result.files
        if report.status != "error" and report.output_name
    ]


def _element_nonempty(ds, keyword: str) -> bool:
    if keyword not in ds:
        return False
    el = ds[keyword]
    # VM == 0 或值为空白均视为缺失
    try:
        if el.VM == 0:
            return False
    except Exception:
        pass
    return str(el.value or "").strip() != ""


def _read_anonymized(path: str):
    """读取匿名输出；匿名化已强制 File Meta，这里要求严格 DICOM 解析。"""
    return pydicom.dcmread(path, force=False)


def validate_outputs(result: JobResult) -> tuple[list[tuple[FileReport, object]], list[dict]]:
    """读取并校验全部成功匿名输出。

    返回 (可建目录的 (报告, 数据集) 列表, 问题明细)；问题非空时调用方必须拒绝输出。
    """
    usable: list[tuple[FileReport, object]] = []
    problems: list[dict] = []

    for report in _successful_reports(result):
        path = _output_path(result, report)
        if not os.path.isfile(path):
            problems.append({
                "output_file": report.output_name,
                "reason": REASON_OUTPUT_MISSING,
                "message": "匿名输出文件在任务目录中不存在（可能已被清理）",
            })
            continue

        try:
            ds = _read_anonymized(path)
        except (InvalidDicomError, OSError, EOFError) as exc:
            problems.append({
                "output_file": report.output_name,
                "reason": REASON_UNREADABLE,
                "message": f"匿名输出不是可读取的 DICOM 文件：{exc}",
            })
            continue
        except Exception as exc:  # 标签编码损坏等
            problems.append({
                "output_file": report.output_name,
                "reason": REASON_UNREADABLE,
                "message": f"读取匿名输出失败：{exc}",
            })
            continue

        missing = [
            keyword
            for keyword in CORE_REQUIRED_KEYWORDS
            if not _element_nonempty(ds, keyword)
        ]
        # File Meta 中的传输语法是目录记录引用必需项
        ts_uid = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
        if not str(ts_uid or "").strip():
            missing.append("TransferSyntaxUID")

        actual_sop = str(getattr(ds, "SOPInstanceUID", "") or "").strip()
        if missing:
            labels = "、".join(
                _TAG_LABELS.get(keyword, keyword) for keyword in missing
            )
            problems.append({
                "output_file": report.output_name,
                "sop_instance_uid": actual_sop or report.sop_instance_uid,
                "reason": REASON_MISSING_TAGS,
                "missing_tags": missing,
                "message": f"缺少建立 DICOMDIR 必需的标签：{labels}",
            })
            continue

        # 文件内实际 SOP UID 必须与任务记录一致（目录层级以文件自身标签为准）
        if report.sop_instance_uid and actual_sop != report.sop_instance_uid:
            problems.append({
                "output_file": report.output_name,
                "sop_instance_uid": actual_sop,
                "reason": REASON_MISSING_TAGS,
                "missing_tags": ["SOPInstanceUID"],
                "message": (
                    "文件内 SOPInstanceUID 与匿名任务记录不一致，无法安全建目录："
                    f"文件内为 {actual_sop}，任务记录为 {report.sop_instance_uid}"
                ),
            })
            continue

        usable.append((report, ds))

    return usable, problems


def _detect_duplicate_sops(
    result: JobResult,
    usable: list[tuple[FileReport, object]],
) -> list[dict]:
    """同一匿名 SOPInstanceUID 对应多份物理输出时逐个 UID 报歧义。

    在**全部成功报告**（含因缺标签 / 不可读未进入 usable 的输出）上按任务记录的
    匿名 SOP UID 归并，确保即使其中一份副本本身还有别的问题，歧义也不会被漏掉；
    能从文件实际读到 UID 时以文件内 UID 为准。
    """
    sop_by_output: dict[str, str] = {}
    for report in _successful_reports(result):
        sop_uid = report.sop_instance_uid or ""
        for _report, ds in usable:
            if _report.output_name == report.output_name:
                sop_uid = str(ds.SOPInstanceUID).strip()
                break
        if sop_uid:
            sop_by_output[report.output_name] = sop_uid

    by_sop: dict[str, list[str]] = {}
    for output_name, sop_uid in sop_by_output.items():
        by_sop.setdefault(sop_uid, []).append(output_name)

    problems: list[dict] = []
    for sop_uid in sorted(by_sop):
        names = sorted(by_sop[sop_uid])
        if len(names) > 1:
            problems.append({
                "output_file": names[0],
                "output_files": names,
                "sop_instance_uid": sop_uid,
                "reason": REASON_AMBIGUOUS_OUTPUT,
                "message": (
                    "同一匿名 SOPInstanceUID 对应多份输出文件，"
                    "DICOMDIR 无法为其分配唯一 File ID"
                ),
            })
    return problems


# --------------------------------------------------------------------------- #
# FileSet 构建（自定义四级记录，只带匿名字段，缺 Type 2 字段也可建目录）
# --------------------------------------------------------------------------- #
def _sort_key(item: tuple[FileReport, object]):
    """固定业务键排序：患者伪名 ID -> Study UID -> Series UID -> SOP UID -> 文件名。

    FileSet 的目录组件编号是实例在树中的兄弟序号；按固定键排序加入，
    可保证同一批匿名输出反复生成相同 File ID。
    """
    report, ds = item
    return (
        str(ds.PatientID).strip(),
        str(getattr(ds, "PatientName", "") or "").strip(),
        str(ds.StudyInstanceUID).strip(),
        str(ds.SeriesInstanceUID).strip(),
        str(ds.SOPInstanceUID).strip(),
        report.output_name,
    )


def _copy_if_present(record: Dataset, ds, keyword: str) -> None:
    """把匿名文件中非空的 Type 2/可选目录字段原样带入记录（不合成身份字段）。"""
    if keyword in ds:
        el = ds[keyword]
        try:
            if el.VM == 0:
                return
        except Exception:
            pass
        if str(el.value or "").strip() != "":
            setattr(record, keyword, el.value)


def _leaf_record_type(ds) -> str:
    """依据 SOP Class UID 选择叶级目录记录类型（复用 pydicom 的内置映射）。

    使用 pydicom.fileset 的 SOP Class -> Directory Record Type 映射，
    与 FileSet.add 的默认行为保持一致；未知 SOP Class 按 IMAGE 处理。
    """
    try:
        from pydicom.fileset import (
            _four_level_record_type,
            _single_level_record_type,
        )
        if _single_level_record_type(ds) != "PATIENT":
            # PALETTE / IMPLANT 等单级记录不属于检查内实例；退回 IMAGE，
            # 仍保留 SOPClassUID 文件引用供离线软件定位
            return "IMAGE"
        return _four_level_record_type(ds)
    except Exception:
        return "IMAGE"


def _build_record_chain(ds) -> RecordNode:
    """依据匿名文件构造 PATIENT -> STUDY -> SERIES -> 叶级记录链。

    仅复制匿名后的伪名 / UID 与非身份性描述字段；不读取、不构造任何原始身份值。
    """
    patient = Dataset()
    patient.DirectoryRecordType = "PATIENT"
    patient.PatientName = ds.PatientName
    patient.PatientID = ds.PatientID

    study = Dataset()
    study.DirectoryRecordType = "STUDY"
    study.StudyInstanceUID = ds.StudyInstanceUID
    for keyword in (
        "StudyDate",
        "StudyTime",
        "StudyID",
        "StudyDescription",
    ):
        _copy_if_present(study, ds, keyword)

    series = Dataset()
    series.DirectoryRecordType = "SERIES"
    series.Modality = ds.Modality
    series.SeriesInstanceUID = ds.SeriesInstanceUID
    for keyword in (
        "SeriesNumber",
        "SeriesDate",
        "SeriesTime",
    ):
        _copy_if_present(series, ds, keyword)

    leaf = Dataset()
    leaf.DirectoryRecordType = _leaf_record_type(ds)
    for keyword in (
        "InstanceNumber",
        "InstanceCreationDate",
        "InstanceCreationTime",
    ):
        _copy_if_present(leaf, ds, keyword)
    # 叶级记录对实例文件的三项引用（FileSet.add_custom 也会自动补，这里先给以
    # 满足 RecordNode 创建时的叶级键要求）
    leaf.ReferencedSOPClassUIDInFile = ds.SOPClassUID
    leaf.ReferencedSOPInstanceUIDInFile = ds.SOPInstanceUID
    leaf.ReferencedTransferSyntaxUIDInFile = ds.file_meta.TransferSyntaxUID

    p_node = RecordNode(patient)
    s_node = RecordNode(study)
    se_node = RecordNode(series)
    l_node = RecordNode(leaf)
    l_node.parent = se_node
    se_node.parent = s_node
    s_node.parent = p_node
    return l_node


def build_file_set(
    result: JobResult, usable: list[tuple[FileReport, object]]
) -> FileSet:
    """按固定顺序把校验通过的匿名输出加入新 FileSet。"""
    fs = FileSet()
    for report, ds in sorted(usable, key=_sort_key):
        leaf = _build_record_chain(ds)
        # add_custom 会把实例暂存到 FileSet 私有临时目录，并补齐引用元素
        fs.add_custom(ds, leaf)
    return fs


# --------------------------------------------------------------------------- #
# ZIP 打包与校验
# --------------------------------------------------------------------------- #
def _zip_add_directory_tree(zf: zipfile.ZipFile, root: str) -> list[str]:
    """把 FileSet 写出的目录树加入 ZIP（arcname 使用 / 分隔），返回条目名列表。"""
    arcnames: list[str] = []
    for current, _dirs, files in os.walk(root):
        for name in sorted(files):
            full = os.path.join(current, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            zf.write(full, arcname=rel)
            arcnames.append(rel)
    return sorted(arcnames)


def write_dicomdir_zip(job) -> tuple[str, DicomDirStats]:
    """构建 DICOMDIR ZIP 到任务目录下的临时文件，返回 (路径, 统计)。

    任何输入问题都抛 DicomDirError；临时目录在失败时清理，不残留半成品。
    """
    result: JobResult = job.result
    usable, problems = validate_outputs(result)
    problems.extend(_detect_duplicate_sops(result, usable))

    if not usable and not problems:
        raise DicomDirError(
            "NO_ANONYMIZED_OUTPUT",
            409,
            "任务没有可用于建立 DICOMDIR 的成功匿名输出",
        )

    base_dir = os.path.dirname(result.output_dir)
    staging = tempfile.mkdtemp(prefix=f"dicomdir-{job.id[:8]}-", dir=base_dir)
    fs_root = os.path.join(staging, "fileset")
    fd, tmp_zip = tempfile.mkstemp(
        prefix=f"dicomdir-{job.id[:8]}-",
        suffix=".zip",
        dir=base_dir,
    )
    os.close(fd)

    try:
        if problems:
            raise DicomDirError(
                "DICOMDIR_BUILD_INCOMPLETE",
                409,
                "匿名输出存在不可读、缺少必需标签或 SOP 歧义文件，已拒绝输出 DICOMDIR 包",
                problems=sorted(problems, key=lambda p: p["output_file"]),
            )

        # ---- 构建并写出 FileSet ------------------------------------------- #
        try:
            fs = build_file_set(result, usable)
            os.makedirs(fs_root, exist_ok=True)
            fs.write(fs_root)
        except DicomDirError:
            raise
        except (ValueError, KeyError, InvalidDicomError) as exc:
            raise DicomDirError(
                "DICOMDIR_BUILD_INCOMPLETE",
                409,
                f"无法依据匿名输出建立目录记录：{exc}",
            ) from exc

        # ---- 重新加载 DICOMDIR，逐条校验引用确实指向 ZIP 将写入的文件 ----- #
        dicomdir_path = os.path.join(fs_root, "DICOMDIR")
        if not os.path.isfile(dicomdir_path):
            raise DicomDirError(
                "DICOMDIR_WRITE_FAILED",
                500,
                "DICOMDIR 文件未能生成",
            )
        fs_check = FileSet(dicomdir_path)
        referenced: set[str] = set()
        patient_keys: set[str] = set()
        study_keys: set[str] = set()
        series_keys: set[str] = set()
        for instance in fs_check:
            file_id = [str(part) for part in instance.ReferencedFileID]
            rel = "/".join(file_id)
            referenced.add(rel)
            if not os.path.isfile(os.path.join(fs_root, rel)):
                raise DicomDirError(
                    "DICOMDIR_REFERENCE_MISSING",
                    500,
                    f"DICOMDIR 记录引用的文件不存在于目录包内：{rel}",
                )
            try:
                patient_keys.add(str(instance.PatientID).strip())
                study_keys.add(str(instance.StudyInstanceUID).strip())
                series_keys.add(str(instance.SeriesInstanceUID).strip())
            except (AttributeError, KeyError):
                pass

        # 实例数必须与输入一致：不允许 FileSet 静默去重导致目录缺项
        if len(referenced) != len(usable):
            raise DicomDirError(
                "DICOMDIR_REFERENCE_MISSING",
                500,
                (
                    f"DICOMDIR 实例数（{len(referenced)}）与匿名输出数"
                    f"（{len(usable)}）不一致，拒绝输出残缺目录包"
                ),
            )

        # ---- 打 ZIP：DICOMDIR 在根，实例为其 File ID 短路径 --------------- #
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            arcnames = set(_zip_add_directory_tree(zf, fs_root))

        if "DICOMDIR" not in arcnames:
            raise DicomDirError(
                "DICOMDIR_WRITE_FAILED",
                500,
                "DICOMDIR 未被写入目录包",
            )
        missing_in_zip = sorted(referenced - arcnames)
        if missing_in_zip:
            raise DicomDirError(
                "DICOMDIR_REFERENCE_MISSING",
                500,
                "DICOMDIR 引用的部分文件未进入 ZIP：" + "、".join(missing_in_zip[:10]),
            )

        stats = DicomDirStats(
            file_count=len(referenced),
            patient_count=len(patient_keys),
            study_count=len(study_keys),
            series_count=len(series_keys),
        )
    except DicomDirError:
        _safe_unlink(tmp_zip)
        raise
    except Exception as exc:
        _safe_unlink(tmp_zip)
        raise DicomDirError(
            "DICOMDIR_WRITE_FAILED",
            500,
            f"构建 DICOMDIR 包失败：{exc}",
        ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return tmp_zip, stats


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
