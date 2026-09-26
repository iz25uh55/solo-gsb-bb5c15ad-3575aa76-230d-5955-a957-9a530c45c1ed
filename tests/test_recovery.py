"""任务清单持久化与重启恢复测试。

覆盖：版本化清单写入（原子替换、不保存稳定密钥）、启动时从工作卷恢复
未过期任务、过期/中断/损坏目录清理、输出文件与索引一致性校验、
恢复后查询/子资源/下载可用、运行中清理与重启恢复同一到期时间。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.jobs import MANIFEST_NAME, MANIFEST_VERSION, JobRegistry
from tests.sample_data import build_sample_zip

SECRET = "recovery-secret-123"


def _new_registry(work_dir: str, ttl: int = 3600) -> JobRegistry:
    reg = JobRegistry(work_dir, ttl)
    reg.setup()
    return reg


def _make_job(reg: JobRegistry, tmp_path, secret: str = SECRET, seed: int = 1):
    zip_path = tmp_path / f"upload-{time.time_ns()}.zip"
    build_sample_zip(str(zip_path), seed=seed)
    return reg.create_and_process(str(zip_path), "study.zip", secret, True)


def _manifest_path(work_dir: str, job_id: str) -> str:
    return os.path.join(work_dir, job_id, MANIFEST_NAME)


def _output_dir(work_dir: str, job_id: str) -> str:
    return os.path.join(work_dir, job_id, "anonymized")


def _first_output_name(job) -> str:
    return next(r.output_name for r in job.result.files if r.output_name)


# --------------------------------------------------------------------------- #
# 清单写入
# --------------------------------------------------------------------------- #
def test_manifest_written_atomically_without_secret(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg = _new_registry(work_dir)
    job = _make_job(reg, tmp_path)
    assert job.status == "ready"

    manifest_path = _manifest_path(work_dir, job.id)
    assert os.path.isfile(manifest_path)
    # 原子写：临时文件已替换，无残留
    assert not os.path.exists(manifest_path + ".tmp")

    raw = open(manifest_path, "rb").read()
    # 清单不保存稳定密钥（只保存指纹）
    assert SECRET.encode() not in raw

    manifest = json.loads(raw)
    assert manifest["version"] == MANIFEST_VERSION
    meta = manifest["job"]
    assert meta["id"] == job.id
    assert meta["secret_fingerprint"] == (
        hashlib.sha256(SECRET.encode("utf-8")).hexdigest()[:12]
    )
    assert meta["status"] == "ready"
    assert meta["ttl"] == 3600
    assert meta["source_name"] == "study.zip"

    # 逐文件预览与诊断完整入清单
    files = manifest["result"]["files"]
    assert len(files) == 6
    ct = next(f for f in files if f["name"] == "patient_1/CT0001.dcm")
    assert ct["status"] == "ok" and ct["changes"]
    bad = next(f for f in files if f["name"] == "broken/bad.dcm")
    assert bad["status"] == "error" and bad["diagnostics"]

    # 结果索引：4 个成功输出，size/sha256 与磁盘一致
    outputs = manifest["outputs"]
    assert len(outputs) == 4
    for name, entry in outputs.items():
        path = os.path.join(_output_dir(work_dir, job.id), name)
        assert os.path.getsize(path) == entry["size"]
        with open(path, "rb") as fh:
            assert hashlib.sha256(fh.read()).hexdigest() == entry["sha256"]


# --------------------------------------------------------------------------- #
# 恢复
# --------------------------------------------------------------------------- #
def test_restart_restores_ready_job(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir)
    job = _make_job(reg1, tmp_path)
    assert job.status == "ready"

    # 模拟重启：新注册表实例从同一工作卷恢复
    reg2 = _new_registry(work_dir)
    restored = reg2.get(job.id)
    assert restored is not None
    assert restored.id == job.id
    assert restored.status == "ready"
    assert restored.source_name == job.source_name
    assert restored.secret_fingerprint == job.secret_fingerprint
    assert restored.created_at == job.created_at
    assert restored.ttl == job.ttl
    # 运行中清理与重启恢复同一到期时间
    assert restored.expires_at == job.expires_at

    result = restored.result
    assert result is not None
    assert len(result.files) == 6
    assert len(result.patients) == 2
    assert result.sops == job.result.sops
    assert result.studies == job.result.studies
    assert result.series == job.result.series
    assert {
        key: p.to_dict() for key, p in result.patients.items()
    } == {key: p.to_dict() for key, p in job.result.patients.items()}
    # 逐文件预览与诊断逐项一致
    assert [r.to_dict() for r in result.files] == [
        r.to_dict() for r in job.result.files
    ]

    # 下载可从恢复结果重建（无需重新匿名化）
    zip_path = reg2.ensure_zip(restored)
    with zipfile.ZipFile(zip_path) as zf:
        assert len(zf.namelist()) == 4


def test_restore_keeps_original_expiry_regardless_of_new_ttl(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir, ttl=3600)
    job = _make_job(reg1, tmp_path)

    # 重启后 TTL 配置变化不影响已持久化任务的到期时间
    reg2 = _new_registry(work_dir, ttl=10)
    restored = reg2.get(job.id)
    assert restored is not None
    assert restored.ttl == 3600
    assert restored.expires_at == job.created_at + 3600


def test_failed_job_restored(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir)
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"definitely not a zip")
    job = reg1.create_and_process(str(bad), "bad.zip", SECRET, True)
    assert job.status == "failed"
    assert job.error

    reg2 = _new_registry(work_dir)
    restored = reg2.get(job.id)
    assert restored is not None
    assert restored.status == "failed"
    assert restored.error == job.error
    assert restored.result is None


def test_runtime_purge_and_restore_share_expiry(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir, ttl=3600)
    job = _make_job(reg1, tmp_path)

    reg2 = _new_registry(work_dir, ttl=3600)
    restored = reg2.get(job.id)
    assert restored is not None
    # 未到期：运行中清理保留
    reg2.purge_expired()
    assert reg2.get(job.id) is not None
    # 到期后：运行中清理按同一到期时间移除
    restored.created_at = time.time() - 7200
    reg2.purge_expired()
    assert reg2.get(job.id) is None
    assert not os.path.isdir(os.path.join(work_dir, job.id))


# --------------------------------------------------------------------------- #
# 启动清理：过期 / 中断 / 损坏
# --------------------------------------------------------------------------- #
def test_expired_job_dir_removed_at_setup(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir, ttl=3600)
    job = _make_job(reg1, tmp_path)

    # 把清单里的创建时间改到 2 小时前（ttl 1 小时）→ 已过期
    manifest_path = _manifest_path(work_dir, job.id)
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["job"]["created_at"] = time.time() - 7200
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    reg2 = _new_registry(work_dir)
    assert reg2.get(job.id) is None
    assert not os.path.isdir(os.path.join(work_dir, job.id))


def test_interrupted_dir_without_manifest_removed(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir)
    job = _make_job(reg1, tmp_path)
    os.unlink(_manifest_path(work_dir, job.id))  # 模拟处理中断（无清单）

    reg2 = _new_registry(work_dir)
    assert reg2.get(job.id) is None
    assert not os.path.isdir(os.path.join(work_dir, job.id))


def test_corrupt_manifest_removed(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir)
    job = _make_job(reg1, tmp_path)
    with open(_manifest_path(work_dir, job.id), "wb") as fh:
        fh.write(b"{ not valid json")

    reg2 = _new_registry(work_dir)
    assert reg2.get(job.id) is None
    assert not os.path.isdir(os.path.join(work_dir, job.id))


def test_manifest_version_mismatch_removed(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir)
    job = _make_job(reg1, tmp_path)
    manifest_path = _manifest_path(work_dir, job.id)
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["version"] = MANIFEST_VERSION + 1
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    reg2 = _new_registry(work_dir)
    assert reg2.get(job.id) is None
    assert not os.path.isdir(os.path.join(work_dir, job.id))


# --------------------------------------------------------------------------- #
# 输出文件与索引一致性
# --------------------------------------------------------------------------- #
def test_missing_output_file_not_restored(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir)
    job = _make_job(reg1, tmp_path)
    os.unlink(os.path.join(_output_dir(work_dir, job.id), _first_output_name(job)))

    reg2 = _new_registry(work_dir)
    assert reg2.get(job.id) is None
    assert not os.path.isdir(os.path.join(work_dir, job.id))


def test_corrupted_output_bytes_not_restored(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir)
    job = _make_job(reg1, tmp_path)
    path = os.path.join(_output_dir(work_dir, job.id), _first_output_name(job))
    with open(path, "ab") as fh:
        fh.write(b"\x00")  # 篡改输出字节

    reg2 = _new_registry(work_dir)
    assert reg2.get(job.id) is None
    assert not os.path.isdir(os.path.join(work_dir, job.id))


def test_extra_output_file_not_restored(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir)
    job = _make_job(reg1, tmp_path)
    with open(os.path.join(_output_dir(work_dir, job.id), "stray.dcm"), "wb") as fh:
        fh.write(b"stray")

    reg2 = _new_registry(work_dir)
    assert reg2.get(job.id) is None
    assert not os.path.isdir(os.path.join(work_dir, job.id))


# --------------------------------------------------------------------------- #
# 临时产物与派生 ZIP
# --------------------------------------------------------------------------- #
def test_stray_temp_artifacts_cleaned_on_setup(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir)
    job = _make_job(reg1, tmp_path)
    job_dir = os.path.join(work_dir, job.id)

    # 工作目录根：跨任务合并 ZIP 的崩溃残留
    stray_root = os.path.join(work_dir, "merged-study-abc123.zip")
    with open(stray_root, "wb") as fh:
        fh.write(b"partial")
    # 任务目录内：子集 ZIP、DICOMDIR 暂存目录、清单临时文件
    stray_subset = os.path.join(job_dir, "study-subset-abc.zip")
    with open(stray_subset, "wb") as fh:
        fh.write(b"partial")
    stray_staging = os.path.join(job_dir, "dicomdir-abc")
    os.makedirs(stray_staging)
    stray_tmp_manifest = os.path.join(job_dir, MANIFEST_NAME + ".tmp")
    with open(stray_tmp_manifest, "w", encoding="utf-8") as fh:
        fh.write("{}")

    reg2 = _new_registry(work_dir)
    assert reg2.get(job.id) is not None
    for path in (stray_root, stray_subset, stray_staging, stray_tmp_manifest):
        assert not os.path.exists(path)


def test_stale_zip_rebuilt_after_restore(tmp_path):
    work_dir = str(tmp_path / "jobs")
    reg1 = _new_registry(work_dir)
    job = _make_job(reg1, tmp_path)
    assert reg1.ensure_zip(job) is not None
    # 崩溃留下的残缺派生 ZIP
    with open(job.zip_path, "wb") as fh:
        fh.write(b"truncated")

    reg2 = _new_registry(work_dir)
    restored = reg2.get(job.id)
    assert restored is not None
    assert not os.path.exists(restored.zip_path)  # 恢复时已移除派生 ZIP
    zip_path = reg2.ensure_zip(restored)
    with zipfile.ZipFile(zip_path) as zf:
        assert len(zf.namelist()) == 4


# --------------------------------------------------------------------------- #
# API 级：重启后查询 / 子资源 / 下载继续可用
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 必须在导入应用配置前设置工作目录
    monkeypatch.setenv("DICOM_ANON_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("DICOM_ANON_JOB_TTL_SECONDS", "3600")
    # 清理已导入模块以让配置重新生效
    import importlib
    import app.config
    import app.jobs
    importlib.reload(app.config)
    importlib.reload(app.jobs)
    import app.main
    importlib.reload(app.main)
    with TestClient(app.main.app) as c:
        yield c


def _sample_zip_bytes(tmp_path, seed: int = 1) -> bytes:
    p = tmp_path / f"sample-{seed}-{time.time_ns()}.zip"
    build_sample_zip(str(p), seed=seed)
    return p.read_bytes()


def _zip_entries(content: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _restart() -> None:
    """模拟服务重启：丢弃内存状态，从工作卷清单恢复。"""
    import app.main

    app.main.registry.setup()


def test_api_survives_restart(client, tmp_path):
    r = client.post(
        "/jobs",
        files={"file": ("study.zip", _sample_zip_bytes(tmp_path), "application/zip")},
        data={"secret": SECRET, "shift_dates": "true"},
    )
    assert r.status_code == 201, r.text
    job = r.json()
    job_id = job["id"]
    before = _zip_entries(client.get(f"/jobs/{job_id}/download").content)

    _restart()

    # 原任务 ID 查询：响应与重启前一致
    r2 = client.get(f"/jobs/{job_id}")
    assert r2.status_code == 200
    assert r2.json() == job

    # 子资源
    r3 = client.get(f"/jobs/{job_id}/files")
    assert r3.status_code == 200
    assert r3.json() == job["files"]

    # 全量下载：内容逐字节一致
    after = _zip_entries(client.get(f"/jobs/{job_id}/download").content)
    assert after == before

    # 实例 PNG 预览子资源
    sop = next(
        f["sop_instance_uid"]
        for f in job["files"]
        if f["status"] == "ok" and f["name"].endswith("CT0001.dcm")
    )
    rp = client.get(f"/jobs/{job_id}/instances/{sop}/preview.png")
    assert rp.status_code == 200
    assert rp.headers["content-type"] == "image/png"

    # DICOMDIR 离线阅片包子资源
    rd = client.get(f"/jobs/{job_id}/dicomdir/download")
    assert rd.status_code == 200
    assert rd.headers["content-type"] == "application/zip"


def test_consistency_check_survives_restart(client, tmp_path):
    ids = []
    for seed in (1, 2):
        r = client.post(
            "/jobs",
            files={
                "file": (f"batch-{seed}.zip", _sample_zip_bytes(tmp_path, seed),
                         "application/zip")
            },
            data={"secret": SECRET},
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])

    before = client.post("/jobs/consistency-check", json={"job_ids": ids})
    assert before.status_code == 200

    _restart()

    after = client.post("/jobs/consistency-check", json={"job_ids": ids})
    assert after.status_code == 200
    assert after.json() == before.json()
    assert after.json()["overall_status"] == "consistent"
