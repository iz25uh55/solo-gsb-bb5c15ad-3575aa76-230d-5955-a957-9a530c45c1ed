"""DICOM 批量匿名化 API。"""
from __future__ import annotations

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from .anonymizer import JobResult
from . import (
    consistency,
    dicomdir,
    dose_events,
    geometry,
    kvp,
    merged_study,
    pagination,
    preview,
    seg_coverage,
    study_subset,
)
from .config import settings
from .jobs import registry
from .schemas import (
    ConsistencyCheckOut,
    ConsistencyCheckRequest,
    ConsistencyDifferenceItem,
    ConsistencyErrorDetail,
    CursorErrorDetail,
    DicomDirErrorDetail,
    DoseEventsErrorDetail,
    DoseEventsOut,
    FileOut,
    GeometryErrorDetail,
    JobOut,
    KvpErrorDetail,
    MergedStudyDownloadRequest,
    MergedStudyErrorDetail,
    PatientOut,
    PreviewErrorDetail,
    SegCoverageErrorDetail,
    SegCoverageOut,
    SeriesGeometryOut,
    SeriesKvpOut,
    SeriesOut,
    StudyOut,
    StudySubsetErrorDetail,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.setup()
    task = asyncio.create_task(registry.purge_loop())
    yield
    task.cancel()


app = FastAPI(
    title="DICOM 批量匿名化 API",
    version="1.0.0",
    description=(
        "上传影像 ZIP，使用用户提供的稳定密钥生成患者级伪名，重写 "
        "Patient/Study/Series/SOP/FrameOfReference UID 与身份字段，"
        "递归更新序列内引用并保持检查关系。提供标签变更预览与匿名化 ZIP 下载，"
        "支持按匿名检查下载子集、跨多个同密钥任务合并下载同一检查、"
        "生成带 DICOMDIR 的离线阅片 ZIP、"
        "按匿名 SOPInstanceUID 渲染单帧灰度 PNG 预览供人工核对，"
        "按匿名 SeriesInstanceUID 统计 CT 管电压（KVP）逐帧分布以发现混用扫描参数，"
        "按匿名 SOPInstanceUID 解析 X-Ray Radiation Dose SR 的 CT 照射事件剂量"
        "（CTDIvol/DLP 与事件 DLP 合计），"
        "按匿名 SOPInstanceUID 统计未压缩 BINARY SEG 的分割覆盖率"
        "（逐帧关联源图像，同源图像/段号多帧并集），"
        "并支持对多个已完成任务做跨批次密钥指纹/伪名/UID 映射一致性核对。"
    ),
    lifespan=lifespan,
)

_executor = ThreadPoolExecutor(max_workers=2)

# 日期平移允许的闭区间边界
DATE_SHIFT_HARD_MIN = -365
DATE_SHIFT_HARD_MAX = 365


def _normalize_shift_range(
    date_shift_min: Optional[int], date_shift_max: Optional[int]
) -> tuple[int, int]:
    """校验日期平移范围：必须成对提供，且 -365 <= min <= max <= 365。"""
    if (date_shift_min is None) != (date_shift_max is None):
        raise HTTPException(
            status_code=422,
            detail="date_shift_min 与 date_shift_max 必须成对提供（同时给出或同时省略）",
        )
    if date_shift_min is None:
        return DATE_SHIFT_HARD_MIN, DATE_SHIFT_HARD_MAX
    if not (
        DATE_SHIFT_HARD_MIN <= date_shift_min <= date_shift_max <= DATE_SHIFT_HARD_MAX
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"日期平移范围非法，须满足 "
                f"{DATE_SHIFT_HARD_MIN} <= date_shift_min <= date_shift_max <= "
                f"{DATE_SHIFT_HARD_MAX}，收到："
                f"min={date_shift_min}, max={date_shift_max}"
            ),
        )
    return date_shift_min, date_shift_max


def _serialize_job(job) -> JobOut:
    if job.status == "failed":
        return JobOut(
            id=job.id,
            status="failed",
            secret_fingerprint=job.secret_fingerprint,
            shift_dates=job.shift_dates,
            date_shift_min=job.date_shift_min,
            date_shift_max=job.date_shift_max,
            source_zip=job.source_name,
            created_at=job.created_at,
            expires_at=job.expires_at,
            file_count=0,
            output_file_count=0,
            error_count=1,
            warning_count=0,
            changes_capped=False,
            patients=[],
            studies=[],
            series=[],
            files=[],
            error=job.error,
        )

    result: JobResult = job.result
    files = [FileOut.model_validate(r.to_dict(include_changes=True)) for r in result.files]
    error_count = sum(r.status == "error" for r in result.files)
    warning_count = sum(r.status == "warning" for r in result.files)
    output_count = sum(bool(r.output_name) for r in result.files)

    return JobOut(
        id=job.id,
        status="ready",
        secret_fingerprint=job.secret_fingerprint,
        shift_dates=job.shift_dates,
        date_shift_min=job.date_shift_min,
        date_shift_max=job.date_shift_max,
        source_zip=job.source_name,
        created_at=job.created_at,
        expires_at=job.expires_at,
        file_count=len(result.files),
        output_file_count=output_count,
        error_count=error_count,
        warning_count=warning_count,
        changes_capped=result.total_changes,
        patients=[
            PatientOut(
                key=p.key,
                new_patient_id=p.new_patient_id,
                new_patient_name=p.new_patient_name,
                date_shift_days=p.date_shift_days,
                file_count=p.file_count,
            )
            for p in sorted(result.patients.values(), key=lambda x: x.key)
        ],
        studies=[
            StudyOut(**v) for v in sorted(
                result.studies.values(), key=lambda x: x["new_study_instance_uid"]
            )
        ],
        series=[
            SeriesOut(**v) for v in sorted(
                result.series.values(), key=lambda x: x["new_series_instance_uid"]
            )
        ],
        files=files,
    )


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/jobs",
    response_model=JobOut,
    status_code=201,
    tags=["jobs"],
    summary="上传影像 ZIP 并创建匿名化任务（同步返回标签变更预览）",
)
async def create_job(
    file: UploadFile = File(..., description="包含 DICOM 文件的 ZIP 压缩包"),
    secret: str = Form(..., description="用户稳定密钥（>=8 字符），用于确定性派生伪名/UID"),
    shift_dates: bool = Form(True, description="是否按患者稳定偏移平移 DA/DT 日期"),
    date_shift_min: Optional[int] = Form(
        None,
        ge=DATE_SHIFT_HARD_MIN,
        le=DATE_SHIFT_HARD_MAX,
        description=(
            "日期平移闭区间下界（天），默认 -365；须与 date_shift_max 成对提供，"
            "并满足 -365 <= min <= max <= 365"
        ),
    ),
    date_shift_max: Optional[int] = Form(
        None,
        ge=DATE_SHIFT_HARD_MIN,
        le=DATE_SHIFT_HARD_MAX,
        description=(
            "日期平移闭区间上界（天），默认 365；须与 date_shift_min 成对提供，"
            "并满足 -365 <= min <= max <= 365"
        ),
    ),
):
    if len(secret) < settings.min_secret_length:
        raise HTTPException(
            status_code=422,
            detail=f"稳定密钥长度至少 {settings.min_secret_length} 个字符",
        )
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=422, detail="仅接受 .zip 压缩包")
    date_shift_min, date_shift_max = _normalize_shift_range(
        date_shift_min, date_shift_max
    )

    # 流式落盘，超过大小上限立即拒绝
    spool = tempfile.NamedTemporaryFile(prefix="upload-", suffix=".zip", delete=False)
    total = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"上传文件超过大小上限 "
                        f"{settings.max_upload_bytes // (1024 * 1024)} MiB"
                    ),
                )
            spool.write(chunk)
        spool.close()
        if total == 0:
            raise HTTPException(status_code=422, detail="上传内容为空")

        loop = asyncio.get_running_loop()
        job = await loop.run_in_executor(
            _executor,
            registry.create_and_process,
            spool.name,
            file.filename,
            secret,
            shift_dates,
            date_shift_min,
            date_shift_max,
        )
    finally:
        if os.path.exists(spool.name):
            try:
                os.unlink(spool.name)
            except OSError:
                pass

    return _serialize_job(job)


@app.post(
    "/jobs/consistency-check",
    response_model=ConsistencyCheckOut,
    tags=["jobs"],
    summary="核对多个已完成任务的密钥指纹、患者稳定伪名与 Study/Series/SOP UID 映射",
    responses={
        400: {"model": CursorErrorDetail,
              "description": "差异明细分页游标非法：篡改/格式错误、过期或跨任务集合/层级复用"},
        404: {"model": ConsistencyErrorDetail, "description": "部分任务不存在或已过期"},
        409: {"model": ConsistencyErrorDetail,
              "description": "部分任务未完成，或任务间密钥指纹不一致"},
    },
)
def consistency_check(body: ConsistencyCheckRequest) -> ConsistencyCheckOut:
    jobs = []
    missing = []
    for job_id in body.job_ids:
        job = registry.get(job_id)
        if job is None:
            missing.append(job_id)
        else:
            jobs.append(job)
    if missing:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "部分任务不存在或已过期",
                "missing_job_ids": missing,
            },
        )

    not_ready = [
        {"id": job.id, "status": job.status, "error": job.error or ""}
        for job in jobs
        if job.status != "ready"
    ]
    if not_ready:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOBS_NOT_COMPLETED",
                "message": "仅支持已完成（status=ready）的任务",
                "not_ready_jobs": not_ready,
            },
        )

    fingerprints = {job.id: job.secret_fingerprint for job in jobs}
    if len(set(fingerprints.values())) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SECRET_FINGERPRINT_MISMATCH",
                "message": "任务使用了不同的稳定密钥（指纹不一致），无法进行一致性核对",
                "fingerprints": fingerprints,
            },
        )

    # 含内部完整差异列表（_all_differences）的报告：计数/状态口径不变
    report = consistency.build_full_report(jobs)
    page = None
    if body.level is not None:
        # 游标必须绑定本次任务集合与筛选层级；篡改/过期/跨条件复用一律 400
        try:
            after = pagination.codec.decode(
                body.cursor or "",
                job_ids=body.job_ids,
                level=body.level,
            )
        except pagination.CursorError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        limit = body.limit or consistency.DEFAULT_DIFF_PAGE_LIMIT
        page = consistency.page_differences(
            report,
            job_ids=body.job_ids,
            level=body.level,
            limit=limit,
            after=after,
        )

    out = consistency.report_without_private(report)
    if page is not None:
        out["items"] = [
            ConsistencyDifferenceItem.model_validate(item) for item in page["items"]
        ]
        out["next_cursor"] = page["next_cursor"]
        out["has_more"] = page["has_more"]
    return ConsistencyCheckOut.model_validate(out)


def _resolve_merge_jobs(job_ids: list[str], study_uid: str) -> list:
    """合并类接口的统一前置校验：存在 -> 已完成 -> 同密钥指纹。"""
    jobs = []
    missing = []
    for job_id in job_ids:
        job = registry.get(job_id)
        if job is None:
            missing.append(job_id)
        else:
            jobs.append(job)
    if missing:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "部分任务不存在或已过期",
                "study_instance_uid": study_uid,
                "missing_job_ids": missing,
            },
        )

    not_ready = [
        {"id": job.id, "status": job.status, "error": job.error or ""}
        for job in jobs
        if job.status != "ready"
    ]
    if not_ready:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOBS_NOT_COMPLETED",
                "message": "仅支持已完成（status=ready）的任务",
                "study_instance_uid": study_uid,
                "not_ready_jobs": not_ready,
            },
        )

    fingerprints = {job.id: job.secret_fingerprint for job in jobs}
    if len(set(fingerprints.values())) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SECRET_FINGERPRINT_MISMATCH",
                "message": "任务使用了不同的稳定密钥（指纹不一致），拒绝跨密钥合并",
                "study_instance_uid": study_uid,
                "fingerprints": fingerprints,
            },
        )
    return jobs


@app.post(
    "/jobs/merged-study/download",
    tags=["jobs"],
    summary="跨 2～5 个同密钥任务合并下载某个匿名检查（递归补全跨任务 SOP 引用，按匿名 UID 去重）",
    responses={
        404: {"model": MergedStudyErrorDetail,
              "description": "部分任务不存在或已过期，或该匿名检查不在任一指定任务的成功输出中"},
        409: {"model": MergedStudyErrorDetail,
              "description": "部分任务未完成、密钥指纹不一致、同一匿名 SOP UID 输出字节冲突，"
                             "或引用目标在所有指定任务中均不存在"},
    },
)
def download_merged_study(body: MergedStudyDownloadRequest, background_tasks: BackgroundTasks):
    jobs = _resolve_merge_jobs(body.job_ids, body.study_instance_uid)

    try:
        zip_path, stats = merged_study.write_merged_study_zip(
            jobs, body.study_instance_uid, settings.work_dir
        )
    except merged_study.MergedStudyError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.detail) from exc

    background_tasks.add_task(_safe_unlink, zip_path)
    # Content-Disposition filename 只允许 token 字符；匿名 UID 为 2.25.<digits>，
    # 点号安全，额外保留 UID 后 24 位避免过长
    safe_uid = "".join(
        ch for ch in body.study_instance_uid if ch.isalnum() or ch in ".-"
    )
    name = f"merged-study-{safe_uid[-24:]}.zip"
    headers = {
        "X-Merged-Job-Count": str(len(jobs)),
        "X-Merged-Study-Initial-File-Count": str(stats.initial_uids),
        "X-Merged-Study-Added-File-Count": str(stats.added_uids),
        "X-Merged-Study-Deduped-Copy-Count": str(stats.deduped_copies),
        "X-Merged-Study-File-Count": str(
            stats.initial_uids + stats.added_uids
        ),
    }
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=name,
        headers=headers,
    )


@app.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    tags=["jobs"],
    summary="获取任务标签变更预览与逐文件诊断",
)
def get_job(job_id: str) -> JobOut:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return _serialize_job(job)


@app.get(
    "/jobs/{job_id}/files",
    response_model=list[FileOut],
    tags=["jobs"],
    summary="仅获取逐文件诊断与变更列表",
)
def list_job_files(job_id: str) -> list[FileOut]:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job.status != "ready":
        return []
    return [FileOut.model_validate(r.to_dict(include_changes=True)) for r in job.result.files]


@app.get(
    "/jobs/{job_id}/download",
    tags=["jobs"],
    summary="下载匿名化后的 ZIP（扁平文件名，不含原始路径）",
)
def download_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job.status != "ready":
        raise HTTPException(status_code=409, detail=f"任务处理失败：{job.error}")
    zip_path = registry.ensure_zip(job)
    error_count = sum(r.status == "error" for r in job.result.files)
    name = f"anonymized-{job.id[:8]}.zip"
    headers = {
        "X-Job-File-Count": str(len(job.result.files)),
        "X-Job-Error-Count": str(error_count),
    }
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=name,
        headers=headers,
    )


@app.get(
    "/jobs/{job_id}/studies/{study_instance_uid}/download",
    tags=["jobs"],
    summary="按匿名 StudyInstanceUID 下载检查子集（自动补全跨检查 SOP 引用闭包）",
    responses={
        404: {"model": StudySubsetErrorDetail,
              "description": "任务不存在，或该匿名检查不在任务的成功输出中"},
        409: {"model": StudySubsetErrorDetail,
              "description": "任务未完成，或引用闭包无法在成功输出内闭合"
                             "（引用缺失 / 一个 SOP UID 对应多个输出文件 / "
                             "成功输出记录对应的物理文件已丢失）"},
    },
)
def download_study_subset(
    job_id: str,
    study_instance_uid: str,
    background_tasks: BackgroundTasks,
):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "任务不存在或已过期",
                "study_instance_uid": study_instance_uid,
            },
        )
    if job.status != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOB_NOT_READY",
                "message": f"任务处理失败：{job.error}",
                "study_instance_uid": study_instance_uid,
            },
        )

    try:
        zip_path, initial_count, added_count = study_subset.write_study_subset_zip(
            job, study_instance_uid
        )
    except study_subset.StudySubsetError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.detail) from exc

    background_tasks.add_task(_safe_unlink, zip_path)
    # Content-Disposition filename 只允许 token 字符；匿名 UID 为 2.25.<digits>，
    # 点号安全，额外保留 UID 后 24 位避免过长
    safe_uid = "".join(ch for ch in study_instance_uid if ch.isalnum() or ch in ".-")
    name = f"study-{safe_uid[-24:]}.zip"
    headers = {
        "X-Study-Initial-File-Count": str(initial_count),
        "X-Study-Added-File-Count": str(added_count),
        "X-Study-Subset-File-Count": str(initial_count + added_count),
    }
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=name,
        headers=headers,
    )


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


@app.get(
    "/jobs/{job_id}/series/{series_instance_uid}/geometry",
    response_model=SeriesGeometryOut,
    tags=["jobs"],
    summary=(
        "按匿名 SeriesInstanceUID 分析单帧切片几何：方向法线投影排序、"
        "中位数层间距与异常缺口标记"
    ),
    responses={
        404: {"model": GeometryErrorDetail,
              "description": "任务不存在/已过期，或该匿名系列不在任务的成功输出中"},
        409: {"model": GeometryErrorDetail,
              "description": "任务未完成，或系列几何冲突：FrameOfReferenceUID 不一致、"
                             "ImageOrientationPatient 方向不一致、几何标签无效、"
                             "投影位置重合、SOP 输出歧义/不可读"},
    },
)
def get_series_geometry(job_id: str, series_instance_uid: str) -> SeriesGeometryOut:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "任务不存在或已过期",
                "series_instance_uid": series_instance_uid,
            },
        )
    if job.status != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOB_NOT_READY",
                "message": f"任务处理失败：{job.error}",
                "series_instance_uid": series_instance_uid,
            },
        )

    try:
        report = geometry.analyze_series_geometry(job, series_instance_uid)
    except geometry.GeometryError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.detail) from exc

    return SeriesGeometryOut.model_validate(report)


@app.get(
    "/jobs/{job_id}/series/{series_instance_uid}/kvp",
    response_model=SeriesKvpOut,
    tags=["jobs"],
    summary=(
        "按匿名 SeriesInstanceUID 统计 CT 管电压（KVP）逐帧分布："
        "各电压帧数、缺失帧数与逐实例明细"
    ),
    responses={
        404: {"model": KvpErrorDetail,
              "description": "任务不存在/已过期，或该匿名系列不在任务的成功输出中"},
        409: {"model": KvpErrorDetail,
              "description": "任务未完成，或 KVP 统计冲突：NumberOfFrames 声明帧数与 "
                             "PerFrameFunctionalGroupsSequence 条目数不符、"
                             "NumberOfFrames 取值非法、同一匿名 SOP UID 对应多份输出、"
                             "匿名输出不可读/缺失"},
    },
)
def get_series_kvp(job_id: str, series_instance_uid: str) -> SeriesKvpOut:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "任务不存在或已过期",
                "series_instance_uid": series_instance_uid,
            },
        )
    if job.status != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOB_NOT_READY",
                "message": f"任务处理失败：{job.error}",
                "series_instance_uid": series_instance_uid,
            },
        )

    try:
        report = kvp.analyze_series_kvp(job, series_instance_uid)
    except kvp.KvpError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.detail) from exc

    return SeriesKvpOut.model_validate(report)


@app.get(
    "/jobs/{job_id}/instances/{sop_instance_uid}/preview.png",
    tags=["jobs"],
    response_class=Response,
    summary="按匿名 SOPInstanceUID 渲染单帧灰度 PNG 预览（人工核对影像内容）",
    responses={
        200: {"content": {"image/png": {"schema": {"type": "string",
                                                   "format": "binary"}}},
              "description": "8 位灰度 PNG（inline 显示）；X-Preview-* 头回传渲染参数"},
        404: {"model": PreviewErrorDetail,
              "description": "任务不存在/已过期，或该匿名 SOP UID 不在任务的成功输出中"},
        409: {"model": PreviewErrorDetail,
              "description": "任务未完成，或同一匿名 SOP UID 对应多份输出文件"},
        422: {"model": PreviewErrorDetail,
              "description": "匿名输出无法解码，或影像类型不支持（压缩/多帧/非 8/16 位 "
                             "MONOCHROME1/2 等）"},
    },
)
def download_instance_preview(job_id: str, sop_instance_uid: str) -> Response:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "任务不存在或已过期",
                "sop_instance_uid": sop_instance_uid,
            },
        )
    if job.status != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOB_NOT_READY",
                "message": f"任务处理失败：{job.error}",
                "sop_instance_uid": sop_instance_uid,
            },
        )

    try:
        rendered = preview.render_preview_png(job, sop_instance_uid)
    except preview.PreviewError as exc:
        raise HTTPException(
            status_code=exc.http_status, detail=exc.detail
        ) from exc

    # Content-Disposition filename 只允许 token 字符；匿名 UID 为 2.25.<digits>，
    # 点号安全，额外保留 UID 后 24 位避免过长；inline 便于浏览器直接显示供核对
    safe_uid = "".join(
        ch for ch in sop_instance_uid if ch.isalnum() or ch in ".-"
    )
    filename = f"preview-{safe_uid[-24:]}.png"
    headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "X-Preview-Rows": str(rendered.rows),
        "X-Preview-Columns": str(rendered.columns),
        "X-Preview-Bits-Allocated": str(rendered.bits_allocated),
        "X-Preview-Bits-Stored": str(rendered.bits_stored),
        "X-Preview-Photometric-Interpretation": rendered.photometric_interpretation,
        "X-Preview-Window": rendered.window_source,
        "X-Preview-Rescale-Slope": repr(rendered.rescale_slope),
        "X-Preview-Rescale-Intercept": repr(rendered.rescale_intercept),
    }
    return Response(content=rendered.png, media_type="image/png", headers=headers)


@app.get(
    "/jobs/{job_id}/instances/{sop_instance_uid}/dose-events",
    response_model=DoseEventsOut,
    tags=["jobs"],
    summary=(
        "按匿名 SOPInstanceUID 解析 X-Ray Radiation Dose SR 的 CT 照射事件剂量："
        "逐事件 CTDIvol/DLP（按内容顺序）与事件 DLP 合计"
    ),
    responses={
        404: {"model": DoseEventsErrorDetail,
              "description": "任务不存在/已过期，该匿名 SOP UID 不在任务的成功输出中，"
                             "或匿名输出文件已不存在"},
        409: {"model": DoseEventsErrorDetail,
              "description": "任务未完成、同一匿名 SOP UID 对应多份输出，或剂量 SR 冲突："
                             "事件内缺项/重复项/非有限数值/单位不符、输出不可读、"
                             "非 X-Ray Radiation Dose SR（不输出部分合计）"},
    },
)
def get_instance_dose_events(job_id: str, sop_instance_uid: str) -> DoseEventsOut:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "任务不存在或已过期",
                "sop_instance_uid": sop_instance_uid,
            },
        )
    if job.status != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOB_NOT_READY",
                "message": f"任务处理失败：{job.error}",
                "sop_instance_uid": sop_instance_uid,
            },
        )

    try:
        report = dose_events.extract_dose_events(job, sop_instance_uid)
    except dose_events.DoseEventsError as exc:
        raise HTTPException(
            status_code=exc.http_status, detail=exc.detail
        ) from exc

    return DoseEventsOut.model_validate(report)


@app.get(
    "/jobs/{job_id}/instances/{sop_instance_uid}/seg-coverage",
    response_model=SegCoverageOut,
    tags=["jobs"],
    summary=(
        "按匿名 SOPInstanceUID 统计未压缩 BINARY SEG 的分割覆盖率："
        "逐帧关联源图像，同（源图像, 段号）多帧并集"
    ),
    responses={
        404: {"model": SegCoverageErrorDetail,
              "description": "任务不存在/已过期，该匿名 SOP UID 不在任务的成功输出中，"
                             "或匿名输出文件已不存在"},
        409: {"model": SegCoverageErrorDetail,
              "description": "任务未完成、同一匿名 SOP UID 对应多份输出，或 SEG 统计冲突："
                             "非 Segmentation/非 BINARY/压缩传输语法、帧数或像素数据异常、"
                             "源图像引用缺失/同帧多源引用/歧义/尺寸不一致（不输出部分统计）"},
    },
)
def get_instance_seg_coverage(job_id: str, sop_instance_uid: str) -> SegCoverageOut:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "任务不存在或已过期",
                "sop_instance_uid": sop_instance_uid,
            },
        )
    if job.status != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOB_NOT_READY",
                "message": f"任务处理失败：{job.error}",
                "sop_instance_uid": sop_instance_uid,
            },
        )

    try:
        report = seg_coverage.analyze_seg_coverage(job, sop_instance_uid)
    except seg_coverage.SegCoverageError as exc:
        raise HTTPException(
            status_code=exc.http_status, detail=exc.detail
        ) from exc

    return SegCoverageOut.model_validate(report)


@app.get(
    "/jobs/{job_id}/dicomdir/download",
    tags=["jobs"],
    summary="下载带 DICOMDIR 的离线阅片 ZIP（按匿名伪名/UID 重建患者-检查-系列-实例层级）",
    responses={
        404: {"description": "任务不存在或已过期"},
        409: {"model": DicomDirErrorDetail,
              "description": "任务未完成、无成功匿名输出，或匿名输出不可读 / "
                             "缺少建目录必需标签 / 同一匿名 SOP UID 对应多份输出"},
    },
)
def download_job_dicomdir(job_id: str, background_tasks: BackgroundTasks):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job.status != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOB_NOT_READY",
                "message": f"任务处理失败：{job.error}",
            },
        )

    try:
        zip_path, stats = dicomdir.write_dicomdir_zip(job)
    except dicomdir.DicomDirError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.detail) from exc

    background_tasks.add_task(_safe_unlink, zip_path)
    name = f"dicomdir-{job.id[:8]}.zip"
    headers = {
        "X-Dicomdir-File-Count": str(stats.file_count),
        "X-Dicomdir-Patient-Count": str(stats.patient_count),
        "X-Dicomdir-Study-Count": str(stats.study_count),
        "X-Dicomdir-Series-Count": str(stats.series_count),
    }
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=name,
        headers=headers,
    )


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "service": "dicom-anon-api",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": [
            "POST /jobs",
            "POST /jobs/consistency-check",
            "POST /jobs/merged-study/download",
            "GET /jobs/{job_id}",
            "GET /jobs/{job_id}/files",
            "GET /jobs/{job_id}/download",
            "GET /jobs/{job_id}/dicomdir/download",
            "GET /jobs/{job_id}/studies/{study_instance_uid}/download",
            "GET /jobs/{job_id}/series/{series_instance_uid}/geometry",
            "GET /jobs/{job_id}/series/{series_instance_uid}/kvp",
            "GET /jobs/{job_id}/instances/{sop_instance_uid}/preview.png",
            "GET /jobs/{job_id}/instances/{sop_instance_uid}/dose-events",
            "GET /jobs/{job_id}/instances/{sop_instance_uid}/seg-coverage",
            "GET /health",
        ],
    }
