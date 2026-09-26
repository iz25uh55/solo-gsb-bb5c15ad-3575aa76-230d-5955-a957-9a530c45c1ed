"""任务存储与处理流水线（线程安全，后台定期清理过期任务）。

持久化与重启恢复：

* 处理完成后把任务元数据、逐文件预览与诊断、结果索引写入任务目录下的
  版本化清单 ``manifest.json``（先写临时文件再原子替换）；清单只保存密钥的
  SHA-256 指纹前 12 位，**绝不保存稳定密钥本身**；
* 启动时扫描工作卷：仅恢复**未过期**、**清单完整**且**输出文件与索引一致**
  （文件名集合、大小与 SHA-256 逐项匹配）的任务；过期或中断残留的目录与
  一次性临时文件一律清理；
* 恢复后原任务 ID 的查询、子资源与下载继续可用，无需重新匿名化；
* 到期时间随清单持久化（``created_at + ttl``），运行中清理与重启恢复使用
  同一份到期时间。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from . import anonymizer
from .config import settings

logger = logging.getLogger(__name__)

# 清单版本：结构变更时递增；版本不符的清单视为无效并清理对应目录
MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"
_MANIFEST_TMP_NAME = MANIFEST_NAME + ".tmp"
# 任务目录/工作目录内可能残留的一次性临时产物前缀
# （检查子集 / DICOMDIR / 跨任务合并的临时 ZIP 与暂存目录、全量 ZIP 构建临时文件）
_STRAY_PREFIXES = ("study-subset-", "dicomdir-", "merged-study-", "zip-build-")


@dataclass
class Job:
    id: str
    source_name: str
    secret_fingerprint: str
    shift_dates: bool
    created_at: float
    ttl: int
    upload_path: str
    extract_dir: str
    output_dir: str
    zip_path: str
    date_shift_min: int = -365
    date_shift_max: int = 365
    status: str = "ready"
    result: Optional[anonymizer.JobResult] = None
    error: Optional[str] = None
    _zip_lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._zip_lock = threading.Lock()

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl


def _remove_quietly(path: str) -> None:
    """删除文件或目录（不存在/失败均静默）。"""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _outputs_index(job: Job) -> dict:
    """结果索引：每个成功输出文件的 size 与 SHA-256（用于恢复时一致性校验）。"""
    index: dict[str, dict] = {}
    if job.result is None:
        return index
    for report in job.result.files:
        if not report.output_name:
            continue
        path = os.path.join(job.output_dir, report.output_name)
        try:
            size = os.path.getsize(path)
            digest = _sha256_file(path)
        except OSError:
            continue
        index[report.output_name] = {"size": size, "sha256": digest}
    return index


def _outputs_consistent(output_dir: str, index: dict) -> bool:
    """输出文件与清单索引一致性校验：集合一致且 size/SHA-256 逐项匹配。"""
    try:
        on_disk = {
            name
            for name in os.listdir(output_dir)
            if os.path.isfile(os.path.join(output_dir, name))
        }
    except OSError:
        return False
    if set(index) != on_disk:
        return False
    for name, meta in index.items():
        path = os.path.join(output_dir, name)
        try:
            if os.path.getsize(path) != int(meta["size"]):
                return False
            if _sha256_file(path) != str(meta["sha256"]):
                return False
        except (OSError, KeyError, TypeError, ValueError):
            return False
    return True


def _result_to_manifest(result: anonymizer.JobResult) -> dict:
    return {
        "total_changes": result.total_changes,
        "patients": {key: p.to_dict() for key, p in result.patients.items()},
        "studies": result.studies,
        "series": result.series,
        "sops": result.sops,
        "files": [r.to_dict(include_changes=True) for r in result.files],
    }


def _result_from_manifest(data: dict, output_dir: str) -> anonymizer.JobResult:
    if not isinstance(data, dict):
        raise ValueError("清单中的 result 必须是对象")
    patients = {}
    for item in data["patients"].values():
        info = anonymizer.PatientInfo.from_dict(item)
        patients[info.key] = info
    return anonymizer.JobResult(
        files=[anonymizer.FileReport.from_dict(f) for f in data["files"]],
        patients=patients,
        studies={str(k): v for k, v in data["studies"].items()},
        series={str(k): v for k, v in data["series"].items()},
        sops={str(k): str(v) for k, v in data["sops"].items()},
        output_dir=output_dir,
        total_changes=bool(data.get("total_changes", False)),
    )


def _manifest_for_job(job: Job) -> dict:
    """版本化清单：任务元数据 + 逐文件预览与诊断 + 结果索引（不含稳定密钥）。"""
    return {
        "version": MANIFEST_VERSION,
        "job": {
            "id": job.id,
            "source_name": job.source_name,
            # 仅密钥指纹（SHA-256 前 12 位），绝不保存稳定密钥本身
            "secret_fingerprint": job.secret_fingerprint,
            "shift_dates": job.shift_dates,
            "date_shift_min": job.date_shift_min,
            "date_shift_max": job.date_shift_max,
            "created_at": job.created_at,
            "ttl": job.ttl,
            "status": job.status,
            "error": job.error,
        },
        "result": (
            _result_to_manifest(job.result) if job.result is not None else None
        ),
        "outputs": _outputs_index(job),
    }


class JobRegistry:
    def __init__(self, work_dir: str, ttl_seconds: int) -> None:
        self.work_dir = work_dir
        self.ttl_seconds = ttl_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ----- 启动：从工作卷恢复 -------------------------------------------- #
    def setup(self) -> None:
        """扫描工作目录：恢复有效任务，清理过期/中断残留。

        只恢复未过期、清单完整且输出文件与索引一致的任务；其余目录
        （无清单、清单损坏/版本不符、已过期、输出不一致）与一次性临时
        文件全部清理。到期时间与运行中清理同为 ``created_at + ttl``。
        """
        os.makedirs(self.work_dir, exist_ok=True)
        now = time.time()
        restored: dict[str, Job] = {}
        for name in sorted(os.listdir(self.work_dir)):
            path = os.path.join(self.work_dir, name)
            if not os.path.isdir(path):
                # 合并 ZIP 等崩溃残留的一次性临时文件
                _remove_quietly(path)
                continue
            job = self._restore_job(path, now)
            if job is None:
                logger.info("清理过期/中断残留的任务目录：%s", path)
                shutil.rmtree(path, ignore_errors=True)
            else:
                restored[job.id] = job
        with self._lock:
            self._jobs = restored
        if restored:
            logger.info("从工作卷恢复 %d 个未过期任务", len(restored))

    def _restore_job(self, base: str, now: float) -> Optional[Job]:
        """从任务目录的版本化清单恢复任务；任何一步校验失败都返回 None。"""
        try:
            with open(os.path.join(base, MANIFEST_NAME), "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            return None  # 无清单或清单损坏：中断残留
        if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
            return None
        try:
            meta = manifest["job"]
            job_id = meta["id"]
            if not isinstance(job_id, str) or job_id != os.path.basename(base):
                return None
            job = Job(
                id=job_id,
                source_name=str(meta["source_name"]),
                secret_fingerprint=str(meta["secret_fingerprint"]),
                shift_dates=bool(meta["shift_dates"]),
                date_shift_min=int(meta["date_shift_min"]),
                date_shift_max=int(meta["date_shift_max"]),
                created_at=float(meta["created_at"]),
                ttl=int(meta["ttl"]),
                upload_path=os.path.join(base, "input.zip"),
                extract_dir=os.path.join(base, "extracted"),
                output_dir=os.path.join(base, "anonymized"),
                zip_path=os.path.join(base, "anonymized.zip"),
            )
        except (KeyError, TypeError, ValueError):
            return None
        # 与运行中清理同一到期时间（created_at + ttl，随清单持久化）
        if now > job.expires_at:
            return None
        status = meta.get("status")
        if status == "ready":
            try:
                result = _result_from_manifest(manifest["result"], job.output_dir)
                outputs = manifest["outputs"]
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
            if not isinstance(outputs, dict) or not _outputs_consistent(
                job.output_dir, outputs
            ):
                return None
            job.result = result
        elif status == "failed":
            job.status = "failed"
            job.error = str(meta.get("error") or "处理失败")
        else:
            return None
        self._clean_strays(base)
        # 全量 ZIP 是派生产物：以校验过的输出为准，下载时按需重建
        _remove_quietly(job.zip_path)
        return job

    @staticmethod
    def _clean_strays(base: str) -> None:
        """清理任务目录内中断残留的一次性临时产物与清单临时文件。"""
        for name in os.listdir(base):
            if name == _MANIFEST_TMP_NAME or name.startswith(_STRAY_PREFIXES):
                _remove_quietly(os.path.join(base, name))

    # ----- 查询 ------------------------------------------------------------ #
    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    # ----- 创建与处理 ------------------------------------------------------- #
    def create_and_process(
        self,
        upload_path: str,
        source_name: str,
        secret: str,
        shift_dates: bool,
        date_shift_min: int = -365,
        date_shift_max: int = 365,
    ) -> Job:
        job_id = uuid.uuid4().hex
        base = os.path.join(self.work_dir, job_id)
        job = Job(
            id=job_id,
            source_name=os.path.basename(source_name),
            secret_fingerprint=hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12],
            shift_dates=shift_dates,
            date_shift_min=date_shift_min,
            date_shift_max=date_shift_max,
            created_at=time.time(),
            ttl=self.ttl_seconds,
            upload_path=os.path.join(base, "input.zip"),
            extract_dir=os.path.join(base, "extracted"),
            output_dir=os.path.join(base, "anonymized"),
            zip_path=os.path.join(base, "anonymized.zip"),
        )
        os.makedirs(base, exist_ok=True)
        shutil.move(upload_path, job.upload_path)
        try:
            os.makedirs(job.extract_dir, exist_ok=True)
            entries = anonymizer.safe_extract_zip(
                job.upload_path,
                job.extract_dir,
                max_total_bytes=settings.max_extracted_bytes,
                max_files=settings.max_extracted_files,
            )
            engine = anonymizer.Anonymizer(
                secret=secret,
                shift_dates=shift_dates,
                date_shift_min=date_shift_min,
                date_shift_max=date_shift_max,
            )
            job.result = engine.run(job.extract_dir, entries, job.output_dir)
        except ValueError as exc:
            job.status = "failed"
            job.error = str(exc)
        except Exception as exc:  # pragma: no cover - 兜底
            job.status = "failed"
            job.error = f"处理失败：{exc}"
        # 处理完成（成功或失败）即落盘版本化清单，供重启后恢复
        try:
            self._write_manifest(job)
        except OSError:
            logger.warning(
                "任务 %s 清单写入失败，结果仅保留在内存中", job_id, exc_info=True
            )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def _write_manifest(self, job: Job) -> None:
        """版本化清单落盘：先写临时文件再原子替换；不保存稳定密钥。"""
        manifest = _manifest_for_job(job)
        tmp = os.path.join(self.work_dir, job.id, _MANIFEST_TMP_NAME)
        final = os.path.join(self.work_dir, job.id, MANIFEST_NAME)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)

    # ----- 下载 ------------------------------------------------------------ #
    def ensure_zip(self, job: Job) -> Optional[str]:
        if job.status != "ready" or job.result is None:
            return None
        with job._zip_lock:
            if not os.path.exists(job.zip_path):
                # 先构建到临时文件再原子替换，避免崩溃留下残缺 ZIP
                tmp = os.path.join(
                    os.path.dirname(job.zip_path),
                    f"zip-build-{uuid.uuid4().hex}.zip",
                )
                try:
                    anonymizer.build_zip(job.result, tmp)
                    os.replace(tmp, job.zip_path)
                except BaseException:
                    _remove_quietly(tmp)
                    raise
        return job.zip_path

    # ----- 到期清理（与重启恢复同一到期时间） ------------------------------ #
    def purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            expired = [jid for jid, job in self._jobs.items() if now > job.expires_at]
            for jid in expired:
                self._jobs.pop(jid)
                shutil.rmtree(
                    os.path.join(self.work_dir, jid), ignore_errors=True
                )

    async def purge_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                self.purge_expired()
            except Exception:
                pass


registry = JobRegistry(settings.work_dir, settings.job_ttl_seconds)
