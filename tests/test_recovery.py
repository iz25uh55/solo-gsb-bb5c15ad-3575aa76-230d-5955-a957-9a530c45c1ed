"""工作卷持久化与重启恢复：版本化清单、原子写入、启动恢复与清理。"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

from tests.sample_data import build_sample_zip

SECRET = "recovery-secret-123456"
TTL = "3600"


def _make_client(monkeypatch, work_dir, ttl=TTL):
    """在同一工作目录上重新加载应用并返回 TestClient（模拟一次服务重启）。"""
    monkeypatch.setenv("DICOM_ANON_WORK_DIR", str(work_dir))
    monkeypatch.setenv("DICOM_ANON_JOB_TTL_SECONDS", ttl)
    import app.config
    import app.jobs

    importlib.reload(app.config)
    importlib.reload(app.jobs)
    import app.main

    importlib.reload(app.main)
    return TestClient(app.main.app)


@pytest.fixture()
def sample_zip_bytes(tmp_path):
    p = tmp_path / "sample.zip"
    build_sample_zip(str(p))
    return p.read_bytes()


def _upload(client, sample_zip_bytes, secret=SECRET):
    r = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": secret, "shift_dates": "true"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _job_dir(work_dir, job_id):
    return os.path.join(str(work_dir), job_id)


def _manifest_path(work_dir, job_id):
    return os.path.join(_job_dir(work_dir, job_id), "manifest.json")


def _read_manifest(work_dir, job_id):
    with open(_manifest_path(work_dir, job_id), "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


def _rewrite_manifest(work_dir, job_id, mutate):
    path = _manifest_path(work_dir, job_id)
    payload = _read_manifest(work_dir, job_id)
    mutate(payload)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


# --------------------------------------------------------------------------- #
# 清单写入
# --------------------------------------------------------------------------- #
def test_manifest_written_without_secret(tmp_path, monkeypatch, sample_zip_bytes):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job = _upload(client, sample_zip_bytes)

    raw = open(_manifest_path(work, job["id"]), "rb").read()
    # 清单不保存稳定密钥（只含 SHA-256 指纹）
    assert SECRET.encode() not in raw
    payload = json.loads(raw.decode("utf-8"))

    assert payload["version"] == 1
    meta = payload["job"]
    assert meta["id"] == job["id"]
    assert meta["source_name"] == "study.zip"
    assert meta["secret_fingerprint"] == job["secret_fingerprint"]
    assert meta["shift_dates"] is True
    assert meta["date_shift_min"] == -365
    assert meta["date_shift_max"] == 365
    assert meta["ttl"] == 3600
    assert meta["created_at"] == job["created_at"]
    assert meta["status"] == "ready"
    assert meta["error"] is None

    # 逐文件预览与诊断、结果索引（患者/检查/系列/SOP 映射）齐全
    result = payload["result"]
    assert len(result["files"]) == job["file_count"] == 6
    assert len(result["patients"]) == 2
    assert result["studies"] and result["series"] and result["sops"]
    ct = next(f for f in result["files"] if f["name"] == "patient_1/CT0001.dcm")
    assert ct["status"] == "ok"
    assert any(c["keyword"] == "PatientID" for c in ct["changes"])
    rt = next(f for f in result["files"] if f["name"].endswith("RS0001.dcm"))
    assert any(d["code"] == "DANGLING_REFERENCE" for d in rt["diagnostics"])

    # 输出索引与磁盘文件一致（文件名/大小/SHA-256）
    out_dir = os.path.join(_job_dir(work, job["id"]), "anonymized")
    outputs = {e["name"]: e for e in result["outputs"]}
    assert set(outputs) == set(os.listdir(out_dir))
    for name, entry in outputs.items():
        with open(os.path.join(out_dir, name), "rb") as fh:
            blob = fh.read()
        assert entry["size"] == len(blob)
        assert entry["sha256"] == hashlib.sha256(blob).hexdigest()

    # 原子替换后无残留清单临时文件
    assert not [
        n for n in os.listdir(_job_dir(work, job["id"]))
        if n.startswith(".manifest-")
    ]


# --------------------------------------------------------------------------- #
# 重启恢复
# --------------------------------------------------------------------------- #
def test_restart_recovers_queries_subresources_and_downloads(
    tmp_path, monkeypatch, sample_zip_bytes
):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job1 = _upload(client, sample_zip_bytes)
        job2 = _upload(client, sample_zip_bytes)
        job_id = job1["id"]
        files_before = client.get(f"/jobs/{job_id}/files").json()
        zip_before = client.get(f"/jobs/{job_id}/download").content

    # 模拟重启：同一工作卷重新加载应用
    with _make_client(monkeypatch, work) as client:
        # 原任务 ID 的查询继续可用，完整预览逐字段一致
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        assert r.json() == job1
        assert client.get(f"/jobs/{job_id}/files").json() == files_before

        # 全量下载字节一致（复用重启前生成的 anonymized.zip）
        r = client.get(f"/jobs/{job_id}/download")
        assert r.status_code == 200
        assert r.content == zip_before

        # 检查子集下载（无悬空引用的 patient_2 检查，引用闭包可闭合）
        study = next(
            s for s in job1["studies"] if s["patient_key"] == "id:P-002"
        )
        r = client.get(
            f"/jobs/{job_id}/studies/{study['new_study_instance_uid']}/download"
        )
        assert r.status_code == 200
        assert r.headers["x-study-subset-file-count"] == "1"

        # 实例 PNG 预览
        sop = next(f["sop_instance_uid"] for f in files_before
                   if f["status"] == "ok")
        r = client.get(f"/jobs/{job_id}/instances/{sop}/preview.png")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"

        # DICOMDIR 离线阅片包
        r = client.get(f"/jobs/{job_id}/dicomdir/download")
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert "DICOMDIR" in zf.namelist()

        # 跨任务一致性核对（两个恢复出来的任务）
        r = client.post(
            "/jobs/consistency-check",
            json={"job_ids": [job1["id"], job2["id"]]},
        )
        assert r.status_code == 200
        assert r.json()["overall_status"] == "consistent"


def test_recovery_does_not_reanonymize(tmp_path, monkeypatch, sample_zip_bytes):
    """恢复只依赖清单 + 匿名输出：原始上传/解压目录删除后仍可查询与下载。"""
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job = _upload(client, sample_zip_bytes)
        job_id = job["id"]

    job_dir = _job_dir(work, job_id)
    os.unlink(os.path.join(job_dir, "input.zip"))
    shutil.rmtree(os.path.join(job_dir, "extracted"))

    with _make_client(monkeypatch, work) as client:
        assert client.get(f"/jobs/{job_id}").status_code == 200
        # anonymized.zip 此前从未生成：恢复后由匿名输出即时重建
        r = client.get(f"/jobs/{job_id}/download")
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert len(zf.namelist()) == 4


def test_failed_job_restored(tmp_path, monkeypatch):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        r = client.post(
            "/jobs",
            files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
            data={"secret": SECRET},
        )
        assert r.status_code == 201
        job = r.json()
        assert job["status"] == "failed"

    with _make_client(monkeypatch, work) as client:
        r = client.get(f"/jobs/{job['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "failed"
        assert body["error"] == job["error"]
        assert client.get(f"/jobs/{job['id']}/download").status_code == 409


# --------------------------------------------------------------------------- #
# 启动清理：过期 / 中断 / 损坏 / 输出不一致
# --------------------------------------------------------------------------- #
def test_expired_job_purged_on_startup(tmp_path, monkeypatch, sample_zip_bytes):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job = _upload(client, sample_zip_bytes)
        job_id = job["id"]

    # 把清单创建时间改到 TTL 之前：启动时按同一到期时间判定为过期
    _rewrite_manifest(
        work, job_id, lambda p: p["job"].update(created_at=p["job"]["created_at"] - 7200)
    )

    with _make_client(monkeypatch, work) as client:
        assert client.get(f"/jobs/{job_id}").status_code == 404
    assert not os.path.exists(_job_dir(work, job_id))


def test_interrupted_job_dir_removed_on_startup(
    tmp_path, monkeypatch, sample_zip_bytes
):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job = _upload(client, sample_zip_bytes)

    # 模拟处理中断留下的半成品目录（无清单）
    orphan = os.path.join(str(work), "ab" * 16)
    os.makedirs(os.path.join(orphan, "extracted"))
    with open(os.path.join(orphan, "input.zip"), "wb") as fh:
        fh.write(b"partial")

    with _make_client(monkeypatch, work) as client:
        # 正常任务不受影响
        assert client.get(f"/jobs/{job['id']}").status_code == 200
    assert not os.path.exists(orphan)


@pytest.mark.parametrize(
    "corrupt",
    [
        "garbage_bytes",
        "empty_object",
        "unknown_version",
        "job_id_mismatch",
        "missing_result",
    ],
)
def test_invalid_manifest_not_restored(
    tmp_path, monkeypatch, sample_zip_bytes, corrupt
):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job = _upload(client, sample_zip_bytes)
        job_id = job["id"]

    path = _manifest_path(work, job_id)
    if corrupt == "garbage_bytes":
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe not json")
    elif corrupt == "empty_object":
        with open(path, "w") as fh:
            fh.write("{}")
    elif corrupt == "unknown_version":
        _rewrite_manifest(work, job_id, lambda p: p.update(version=999))
    elif corrupt == "job_id_mismatch":
        _rewrite_manifest(work, job_id, lambda p: p["job"].update(id="cd" * 16))
    elif corrupt == "missing_result":
        _rewrite_manifest(work, job_id, lambda p: p.update(result=None))

    with _make_client(monkeypatch, work) as client:
        assert client.get(f"/jobs/{job_id}").status_code == 404
    assert not os.path.exists(_job_dir(work, job_id))


def test_output_byte_tampered_not_restored(
    tmp_path, monkeypatch, sample_zip_bytes
):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job = _upload(client, sample_zip_bytes)
        job_id = job["id"]

    # 同尺寸篡改一个字节：大小不变，SHA-256 与索引不一致
    out_dir = os.path.join(_job_dir(work, job_id), "anonymized")
    victim = os.path.join(out_dir, sorted(os.listdir(out_dir))[0])
    with open(victim, "rb") as fh:
        blob = bytearray(fh.read())
    blob[len(blob) // 2] ^= 0xFF
    with open(victim, "wb") as fh:
        fh.write(bytes(blob))

    with _make_client(monkeypatch, work) as client:
        assert client.get(f"/jobs/{job_id}").status_code == 404
    assert not os.path.exists(_job_dir(work, job_id))


def test_output_file_missing_not_restored(
    tmp_path, monkeypatch, sample_zip_bytes
):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job = _upload(client, sample_zip_bytes)
        job_id = job["id"]

    out_dir = os.path.join(_job_dir(work, job_id), "anonymized")
    os.unlink(os.path.join(out_dir, sorted(os.listdir(out_dir))[0]))

    with _make_client(monkeypatch, work) as client:
        assert client.get(f"/jobs/{job_id}").status_code == 404
    assert not os.path.exists(_job_dir(work, job_id))


def test_output_file_extra_not_restored(tmp_path, monkeypatch, sample_zip_bytes):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job = _upload(client, sample_zip_bytes)
        job_id = job["id"]

    out_dir = os.path.join(_job_dir(work, job_id), "anonymized")
    with open(os.path.join(out_dir, "stray.dcm"), "wb") as fh:
        fh.write(b"stray")

    with _make_client(monkeypatch, work) as client:
        assert client.get(f"/jobs/{job_id}").status_code == 404
    assert not os.path.exists(_job_dir(work, job_id))


def test_stale_temp_artifacts_cleaned_on_startup(
    tmp_path, monkeypatch, sample_zip_bytes
):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job = _upload(client, sample_zip_bytes)
        job_id = job["id"]

    job_dir = _job_dir(work, job_id)
    stale = [
        os.path.join(str(work), "merged-study-deadbeef.zip"),  # 根目录合并临时 ZIP
        os.path.join(job_dir, "study-subset-12345678-dead.zip"),
        os.path.join(job_dir, "dicomdir-12345678-staging"),    # 阅片包暂存目录
        os.path.join(job_dir, ".manifest-abc123.tmp"),         # 清单临时文件
    ]
    os.makedirs(stale[2])
    for path in (stale[0], stale[1], stale[3]):
        with open(path, "wb") as fh:
            fh.write(b"stale")

    with _make_client(monkeypatch, work) as client:
        # 有效任务正常恢复，临时产物全部清理
        assert client.get(f"/jobs/{job_id}").status_code == 200
    for path in stale:
        assert not os.path.exists(path), path


# --------------------------------------------------------------------------- #
# 运行中清理与重启恢复使用同一到期时间
# --------------------------------------------------------------------------- #
def test_runtime_purge_uses_same_expiry_as_manifest(
    tmp_path, monkeypatch, sample_zip_bytes
):
    work = tmp_path / "jobs"
    with _make_client(monkeypatch, work) as client:
        job = _upload(client, sample_zip_bytes)
        job_id = job["id"]

    with _make_client(monkeypatch, work) as client:
        import app.jobs as jobs_mod
        from app.main import registry

        restored = registry.get(job_id)
        assert restored is not None
        # 到期时间 = 清单中的 created_at + ttl，与重启前 API 返回值一致
        assert restored.created_at == job["created_at"]
        assert restored.ttl == 3600
        assert restored.expires_at == job["expires_at"]

        # 运行中清理使用同一到期时间：越过 expires_at 即被清理
        monkeypatch.setattr(
            jobs_mod.time, "time", lambda: restored.expires_at + 1
        )
        registry.purge_expired()
        assert registry.get(job_id) is None
        assert not os.path.exists(_job_dir(work, job_id))
