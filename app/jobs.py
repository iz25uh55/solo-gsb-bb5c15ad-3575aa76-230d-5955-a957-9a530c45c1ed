"""任务存储与处理流水线（线程安全，后台定期清理过期任务）。

工作卷持久化与重启恢复：任务处理完成后把任务元数据、逐文件预览与诊断、
结果索引与匿名输出文件索引写入任务目录下的版本化清单（manifest.json，
先写临时文件再原子替换，清单不保存稳定密钥）；服务启动时扫描工作卷中的
任务目录，只恢复未过期、清单完整且匿名输出与索引一致的任务（原任务 ID
不变，查询/子资源/下载直接可用，无需重新匿名化），并清理过期或中断
残留的目录。运行中清理与重启恢复使用同一到期时间（created_at + ttl）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from . import anonymizer, manifest
from .config import settings

logger = logging.getLogger(__name__)

# 任务目录布局（恢复时按此重建路径）
_UPLOAD_NAME = "input.zip"
_EXTRACT_DIR_NAME = "extracted"
_OUTPUT_DIR_NAME = "anonymized"
_ZIP_NAME = "anonymized.zip"

# 工作卷根目录 / 任务目录内可能出现的临时产物前缀（中断的合并下载、
# 检查子集/阅片包构建、清单原子写入），启动恢复时一并清理
_ROOT_TEMP_PREFIXES = ("merged-study-",)
_JOB_TEMP_PREFIXES = ("study-subset-", "dicomdir-", manifest.TMP_PREFIX)


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


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


class JobRegistry:
    def __init__(self, work_dir: str, ttl_seconds: int) -> None:
        self.work_dir = work_dir
        self.ttl_seconds = ttl_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def setup(self) -> None:
        """初始化工作卷：恢复可恢复的任务，清理过期/中断残留。

        逐项扫描任务目录（不再整体清空工作卷）：

        * 未过期、清单完整且匿名输出与索引一致 -> 按原任务 ID 恢复；
        * 已过期、清单缺失/损坏/版本不符、输出与索引不一致（含处理中断
          留下的半成品目录）-> 整个目录清理；
        * 工作卷根目录的临时文件（如中断的合并 ZIP）一并清理。

        到期判定与运行中清理使用同一到期时间（``Job.expires_at``）。
        """
        os.makedirs(self.work_dir, exist_ok=True)
        now = time.time()
        restored: dict[str, Job] = {}
        purged = 0
        for name in sorted(os.listdir(self.work_dir)):
            path = os.path.join(self.work_dir, name)
            if not os.path.isdir(path):
                # 根目录下中断合并下载留下的临时 ZIP
                if name.startswith(_ROOT_TEMP_PREFIXES):
                    _silent_unlink(path)
                continue
            job = self._restore_job(name, path, now)
            if job is None:
                purged += 1
                shutil.rmtree(path, ignore_errors=True)
                continue
            self._clean_temp_artifacts(path)
            restored[job.id] = job
        with self._lock:
            self._jobs = restored
        if restored or purged:
            logger.info(
                "工作卷恢复：%d 个任务已恢复，%d 个残留目录已清理",
                len(restored),
                purged,
            )

    def _restore_job(self, job_id: str, job_dir: str, now: float) -> Optional[Job]:
        """按清单从任务目录恢复任务；清单无效/已过期/输出不一致时返回 None。"""
        output_dir = os.path.join(job_dir, _OUTPUT_DIR_NAME)
        try:
            meta, result = manifest.read_manifest(job_dir, job_id, output_dir)
        except manifest.ManifestError as exc:
            logger.info("任务目录 %s 不可恢复（%s），将清理", job_id, exc)
            return None
        job = Job(
            id=meta["id"],
            source_name=meta["source_name"],
            secret_fingerprint=meta["secret_fingerprint"],
            shift_dates=meta["shift_dates"],
            date_shift_min=meta["date_shift_min"],
            date_shift_max=meta["date_shift_max"],
            created_at=meta["created_at"],
            ttl=meta["ttl"],
            upload_path=os.path.join(job_dir, _UPLOAD_NAME),
            extract_dir=os.path.join(job_dir, _EXTRACT_DIR_NAME),
            output_dir=output_dir,
            zip_path=os.path.join(job_dir, _ZIP_NAME),
            status=meta["status"],
            result=result,
            error=meta["error"],
        )
        # 与运行中清理同一到期时间：expires_at = created_at + ttl
        if now > job.expires_at:
            return None
        return job

    @staticmethod
    def _clean_temp_artifacts(job_dir: str) -> None:
        """清理任务目录内中断的下载/建目录/清单写入留下的临时产物。"""
        for name in os.listdir(job_dir):
            if not name.startswith(_JOB_TEMP_PREFIXES):
                continue
            path = os.path.join(job_dir, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                _silent_unlink(path)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

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
            upload_path=os.path.join(base, _UPLOAD_NAME),
            extract_dir=os.path.join(base, _EXTRACT_DIR_NAME),
            output_dir=os.path.join(base, _OUTPUT_DIR_NAME),
            zip_path=os.path.join(base, _ZIP_NAME),
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
        # 处理完成（ready/failed）后落版本化清单，供重启恢复；清单只含密钥
        # 指纹，不保存稳定密钥。清单写失败不影响本次内存中的结果，仅降级为
        # “重启后不可恢复”。
        try:
            manifest.write_manifest(job, base)
        except Exception as exc:  # pragma: no cover - 磁盘故障等兜底
            logger.warning("任务 %s 清单写入失败（重启后不可恢复）：%s", job_id, exc)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def ensure_zip(self, job: Job) -> Optional[str]:
        if job.status != "ready" or job.result is None:
            return None
        with job._zip_lock:
            if not os.path.exists(job.zip_path):
                anonymizer.build_zip(job.result, job.zip_path)
        return job.zip_path

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
