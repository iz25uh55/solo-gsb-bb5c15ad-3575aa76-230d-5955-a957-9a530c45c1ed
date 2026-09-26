"""任务清单（manifest）的持久化、校验与恢复。

任务处理完成后，把任务元数据、逐文件预览与诊断、结果索引（患者/检查/系列/
SOP 映射）以及匿名输出文件索引（文件名/大小/SHA-256）写入任务目录下的
``manifest.json``：

* **版本化**：``version`` 标识格式版本，无法识别的版本一律视为无效清单；
* **原子写入**：先写同目录临时文件再 ``os.replace``，进程崩溃不会留下半份清单；
* **不含稳定密钥**：清单只保存密钥指纹（SHA-256 前 12 位），稳定密钥本身
  从不落盘；
* **输出可核对**：恢复时逐一核对匿名输出目录的文件集合、大小与 SHA-256，
  与索引不一致的任务不恢复。

只有“未过期 + 清单完整 + 输出与索引一致”的任务才可恢复；其余目录
（处理中断残留、清单损坏/版本不符、输出被改动、已过期）由调用方清理。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Optional

from .anonymizer import Change, Diagnostic, FileReport, JobResult, PatientInfo

# 清单格式版本；结构变更时递增，无法识别的版本视为无效清单
MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"
# 清单原子写入的临时文件前缀（同目录、同文件系统，随后 os.replace）
TMP_PREFIX = ".manifest-"
_SHA256_HEX_LEN = 64


class ManifestError(Exception):
    """清单缺失/损坏/版本不符/输出与索引不一致；调用方据此放弃恢复并清理目录。"""


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _index_outputs(output_dir: str) -> list[dict]:
    """匿名输出目录的扁平文件索引（文件名/大小/SHA-256），按文件名排序。"""
    entries: list[dict] = []
    if not os.path.isdir(output_dir):
        return entries
    for name in sorted(os.listdir(output_dir)):
        path = os.path.join(output_dir, name)
        if not os.path.isfile(path):
            continue
        entries.append(
            {
                "name": name,
                "size": os.path.getsize(path),
                "sha256": _sha256_file(path),
            }
        )
    return entries


def _result_to_json(result: JobResult) -> dict:
    return {
        "total_changes": bool(result.total_changes),
        "patients": [
            {
                "key": p.key,
                "new_patient_id": p.new_patient_id,
                "new_patient_name": p.new_patient_name,
                "date_shift_days": p.date_shift_days,
                "file_count": p.file_count,
            }
            for p in sorted(result.patients.values(), key=lambda x: x.key)
        ],
        "studies": sorted(
            result.studies.values(), key=lambda v: v["new_study_instance_uid"]
        ),
        "series": sorted(
            result.series.values(), key=lambda v: v["new_series_instance_uid"]
        ),
        "sops": dict(sorted(result.sops.items())),
        "files": [r.to_dict(include_changes=True) for r in result.files],
        "outputs": _index_outputs(result.output_dir),
    }


def build_manifest(job) -> dict:
    """组装清单字典；只含密钥指纹，绝不包含稳定密钥本身。"""
    return {
        "version": MANIFEST_VERSION,
        "job": {
            "id": job.id,
            "source_name": job.source_name,
            "secret_fingerprint": job.secret_fingerprint,
            "shift_dates": job.shift_dates,
            "date_shift_min": job.date_shift_min,
            "date_shift_max": job.date_shift_max,
            "created_at": job.created_at,
            "ttl": job.ttl,
            "status": job.status,
            "error": job.error,
        },
        "result": _result_to_json(job.result) if job.result is not None else None,
    }


def _fsync_dir(path: str) -> None:
    """尽力而为的目录 fsync：保证 rename 结果落盘（不支持的平台跳过）。"""
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_manifest(job, job_dir: str) -> str:
    """把任务清单原子写入 ``job_dir/manifest.json``，返回清单路径。

    先写同目录临时文件（同一文件系统，mkstemp 默认 0600 权限）再
    ``os.replace``：任何时刻清单要么不存在、要么是完整的一份，
    崩溃不会留下半份 JSON。
    """
    data = json.dumps(
        build_manifest(job), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    manifest_path = os.path.join(job_dir, MANIFEST_NAME)
    fd, tmp_path = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=".tmp", dir=job_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, manifest_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _fsync_dir(job_dir)
    return manifest_path


# --------------------------------------------------------------------------- #
# 读取与校验
# --------------------------------------------------------------------------- #
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _opt_str(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _is_flat_name(value: Any) -> bool:
    """扁平文件名（无路径分隔符、非 . / ..）：防止被篡改的清单指到目录外。"""
    return (
        _is_str(value)
        and value not in ("", ".", "..")
        and "/" not in value
        and "\\" not in value
    )


def _parse_meta(raw: Any, expected_job_id: str) -> dict:
    _require(isinstance(raw, dict), "清单缺少任务元数据")
    meta = {
        "id": raw.get("id"),
        "source_name": raw.get("source_name"),
        "secret_fingerprint": raw.get("secret_fingerprint"),
        "shift_dates": raw.get("shift_dates"),
        "date_shift_min": raw.get("date_shift_min"),
        "date_shift_max": raw.get("date_shift_max"),
        "created_at": raw.get("created_at"),
        "ttl": raw.get("ttl"),
        "status": raw.get("status"),
        "error": raw.get("error"),
    }
    _require(
        _is_str(meta["id"]) and meta["id"] == expected_job_id,
        "清单任务 ID 与目录名不一致",
    )
    _require(_is_str(meta["source_name"]), "source_name 非法")
    _require(_is_str(meta["secret_fingerprint"]), "secret_fingerprint 非法")
    _require(isinstance(meta["shift_dates"], bool), "shift_dates 非法")
    _require(
        _is_int(meta["date_shift_min"]) and _is_int(meta["date_shift_max"]),
        "date_shift_min/max 非法",
    )
    _require(
        _is_num(meta["created_at"]) and meta["created_at"] > 0, "created_at 非法"
    )
    _require(_is_int(meta["ttl"]) and meta["ttl"] >= 0, "ttl 非法")
    _require(meta["status"] in ("ready", "failed"), "status 非法")
    _require(_opt_str(meta["error"]), "error 非法")
    _require(
        meta["status"] != "failed" or _is_str(meta["error"]),
        "失败任务缺少 error",
    )
    meta["created_at"] = float(meta["created_at"])
    return meta


def _parse_patients(raw: Any) -> dict[str, PatientInfo]:
    _require(isinstance(raw, list), "patients 非法")
    patients: dict[str, PatientInfo] = {}
    for item in raw:
        _require(isinstance(item, dict), "patients 条目非法")
        key = item.get("key")
        _require(_is_str(key) and key not in patients, "患者键非法或重复")
        _require(
            _is_str(item.get("new_patient_id"))
            and _is_str(item.get("new_patient_name"))
            and _is_int(item.get("date_shift_days"))
            and _is_int(item.get("file_count")),
            "患者条目字段非法",
        )
        patients[key] = PatientInfo(
            key=key,
            new_patient_id=item["new_patient_id"],
            new_patient_name=item["new_patient_name"],
            date_shift_days=item["date_shift_days"],
            file_count=item["file_count"],
        )
    return patients


_STUDY_FIELDS = (
    "new_study_instance_uid",
    "old_study_instance_uid",
    "patient_key",
    "file_count",
)
_SERIES_FIELDS = (
    "new_series_instance_uid",
    "old_series_instance_uid",
    "new_study_instance_uid",
    "file_count",
)


def _parse_uid_index(
    raw: Any, key_field: str, fields: tuple[str, ...], what: str
) -> dict[str, dict]:
    _require(isinstance(raw, list), f"{what} 非法")
    out: dict[str, dict] = {}
    for item in raw:
        _require(isinstance(item, dict), f"{what} 条目非法")
        for field in fields:
            if field == "file_count":
                _require(_is_int(item.get(field)), f"{what}.{field} 非法")
            else:
                _require(_is_str(item.get(field)), f"{what}.{field} 非法")
        key = item[key_field]
        _require(key not in out, f"{what} 键重复")
        out[key] = {field: item[field] for field in fields}
    return out


def _parse_sops(raw: Any) -> dict[str, str]:
    _require(isinstance(raw, dict), "sops 非法")
    for old, new in raw.items():
        _require(_is_str(old) and _is_str(new), "sops 条目非法")
    return dict(raw)


def _parse_diagnostics(raw: Any) -> list[Diagnostic]:
    _require(isinstance(raw, list), "diagnostics 非法")
    out: list[Diagnostic] = []
    for item in raw:
        _require(
            isinstance(item, dict)
            and _is_str(item.get("code"))
            and item.get("severity") in ("info", "warning", "error")
            and _is_str(item.get("message")),
            "诊断条目非法",
        )
        out.append(
            Diagnostic(
                code=item["code"], severity=item["severity"], message=item["message"]
            )
        )
    return out


def _parse_changes(raw: Any) -> list[Change]:
    _require(isinstance(raw, list), "changes 非法")
    out: list[Change] = []
    for item in raw:
        _require(
            isinstance(item, dict)
            and _opt_str(item.get("tag"))
            and _is_str(item.get("keyword"))
            and _is_str(item.get("action"))
            and _opt_str(item.get("old"))
            and _opt_str(item.get("new"))
            and _is_str(item.get("path")),
            "变更条目非法",
        )
        out.append(
            Change(
                tag=item["tag"],
                keyword=item["keyword"],
                action=item["action"],
                old=item["old"],
                new=item["new"],
                path=item["path"],
            )
        )
    return out


def _parse_files(raw: Any) -> list[FileReport]:
    _require(isinstance(raw, list), "files 非法")
    reports: list[FileReport] = []
    for item in raw:
        _require(isinstance(item, dict), "files 条目非法")
        _require(_is_str(item.get("name")), "文件名非法")
        _require(_is_int(item.get("entry_index")), "entry_index 非法")
        _require(
            item.get("status") in ("ok", "warning", "error", "skipped"),
            "文件状态非法",
        )
        for field in (
            "output_name",
            "sop_instance_uid",
            "study_instance_uid",
            "series_instance_uid",
            "patient_key",
        ):
            _require(_opt_str(item.get(field)), f"{field} 非法")
        output_name = item.get("output_name")
        _require(
            output_name is None or _is_flat_name(output_name), "输出文件名非法"
        )
        _require(
            isinstance(item.get("changes_truncated"), bool),
            "changes_truncated 非法",
        )
        reports.append(
            FileReport(
                name=item["name"],
                status=item["status"],
                entry_index=item["entry_index"],
                output_name=output_name,
                sop_instance_uid=item.get("sop_instance_uid"),
                study_instance_uid=item.get("study_instance_uid"),
                series_instance_uid=item.get("series_instance_uid"),
                patient_key=item.get("patient_key"),
                diagnostics=_parse_diagnostics(item.get("diagnostics")),
                changes=_parse_changes(item.get("changes")),
                changes_truncated=item["changes_truncated"],
            )
        )
    return reports


def _parse_outputs(raw: Any) -> list[dict]:
    _require(isinstance(raw, list), "outputs 非法")
    entries: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        _require(isinstance(item, dict), "outputs 条目非法")
        name = item.get("name")
        _require(
            _is_flat_name(name) and name not in seen, "输出索引文件名非法或重复"
        )
        _require(
            _is_int(item.get("size")) and item["size"] >= 0, "输出索引大小非法"
        )
        sha256 = item.get("sha256")
        _require(
            _is_str(sha256)
            and len(sha256) == _SHA256_HEX_LEN
            and all(ch in "0123456789abcdef" for ch in sha256),
            "输出索引 SHA-256 非法",
        )
        seen.add(name)
        entries.append({"name": name, "size": item["size"], "sha256": sha256})
    return entries


def _verify_outputs(
    index: list[dict], result: JobResult, output_dir: str
) -> None:
    """匿名输出目录必须与清单索引完全一致（文件集合、大小、SHA-256）。"""
    _require(os.path.isdir(output_dir), "匿名输出目录缺失")
    disk: set[str] = set()
    for name in os.listdir(output_dir):
        _require(
            os.path.isfile(os.path.join(output_dir, name)),
            "输出目录中存在非文件条目",
        )
        disk.add(name)
    indexed = {entry["name"] for entry in index}
    _require(disk == indexed, "输出文件集合与索引不一致")
    # 逐文件结果引用的输出名必须都在索引内（结果索引与输出索引一致）
    for report in result.files:
        if report.output_name is not None:
            _require(
                report.output_name in indexed, "逐文件结果引用了索引外的输出文件"
            )
    for entry in index:
        path = os.path.join(output_dir, entry["name"])
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise ManifestError(f"输出文件不可读：{exc}") from exc
        _require(
            size == entry["size"], f"输出文件大小与索引不一致：{entry['name']}"
        )
        _require(
            _sha256_file(path) == entry["sha256"],
            f"输出文件摘要与索引不一致：{entry['name']}",
        )


def _parse_result(raw: Any, output_dir: str) -> JobResult:
    _require(isinstance(raw, dict), "结果索引非法")
    _require(isinstance(raw.get("total_changes"), bool), "total_changes 非法")
    result = JobResult(
        files=_parse_files(raw.get("files")),
        patients=_parse_patients(raw.get("patients")),
        studies=_parse_uid_index(
            raw.get("studies"), "new_study_instance_uid", _STUDY_FIELDS, "studies"
        ),
        series=_parse_uid_index(
            raw.get("series"), "new_series_instance_uid", _SERIES_FIELDS, "series"
        ),
        sops=_parse_sops(raw.get("sops")),
        output_dir=output_dir,
        total_changes=raw["total_changes"],
    )
    _verify_outputs(_parse_outputs(raw.get("outputs")), result, output_dir)
    return result


def read_manifest(
    job_dir: str, expected_job_id: str, output_dir: str
) -> tuple[dict, Optional[JobResult]]:
    """读取并校验任务清单，返回 (任务元数据, JobResult | None)。

    清单缺失/损坏/版本不符、任务 ID 与目录名不一致、结果索引非法或匿名输出
    与索引不一致时抛 ``ManifestError``；失败任务（status=failed）的结果为
    ``None``。
    """
    path = os.path.join(job_dir, MANIFEST_NAME)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ManifestError(f"清单不可读：{exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"清单 JSON 损坏：{exc}") from exc
    _require(isinstance(payload, dict), "清单顶层结构非法")
    _require(
        payload.get("version") == MANIFEST_VERSION, "清单版本不受支持"
    )
    meta = _parse_meta(payload.get("job"), expected_job_id)
    result = None
    if meta["status"] == "ready":
        result = _parse_result(payload.get("result"), output_dir)
    return meta, result
