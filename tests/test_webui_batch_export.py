"""批量下载：一次请求把多个工程的 SVP（或产物文件）打成一个 zip。

契约（前端只认这些）：
- `GET /api/exports/batch?ids=a,b,c` → `application/zip`，每个工程一个 `<歌名>.svp`；
- 压缩包里总有一份 `_manifest.json`：**导出成功的**（含音符数/轨数）与**被跳过的**（含原因）；
- `ids` 里有不存在的工程 → 404（别默默吞掉，那会让"少了三首"没法查）；
- 一首都没导出来 → 409（不是 200 + 空包）；
- 同名工程 → `同名.svp` / `同名-2.svp`（不覆盖）；
- `kind=files` → 每个工程的产物按 `<歌名>/<相对路径>` 摊开（不是套娃 zip）。
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from support_webui import (fake_runners, job_internal, make_client, upload,
                           wait_for_state)

#: SVP 是 JSON 文件（1.x 末尾还有个 \x00），用它判断"真的是 SVP"而不是占位文件。
SVP_MAGIC = b"{"


def transcribed(client, name: str) -> dict:
    """建一个扒完谱的工程并等它落终态。"""
    job = upload(client, name=name, plan="transcribe").json()
    wait_for_state(client, job["id"], {"done"})
    return job


def batch_zip(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert ".zip" in response.headers["content-disposition"]
    return zipfile.ZipFile(io.BytesIO(response.content))


def manifest_of(zf: zipfile.ZipFile) -> dict:
    return json.loads(zf.read("_manifest.json").decode("utf-8"))


def test_batch_export_packs_one_svp_per_job(tmp_path):
    pytest.importorskip("libresvip", reason="可选依赖：pip install -e .[svp]")
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        first = transcribed(client, "甲.wav")
        second = transcribed(client, "乙.wav")

        response = client.get("/api/exports/batch",
                              params={"ids": f"{first['id']},{second['id']}"})
        zf = batch_zip(response)
        names = set(zf.namelist())
        assert {"甲.svp", "乙.svp", "_manifest.json"} <= names
        assert zf.read("甲.svp").startswith(SVP_MAGIC)

        manifest = manifest_of(zf)
        assert [item["name"] for item in manifest["exported"]] == ["甲", "乙"]
        assert manifest["kind"] == "svp"
        assert manifest["parts"] == ["vocal"]
        assert manifest["exported"][0]["notes"] > 0


def test_batch_export_skips_jobs_without_artifacts(tmp_path):
    pytest.importorskip("libresvip", reason="可选依赖：pip install -e .[svp]")
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        done = transcribed(client, "有谱.wav")
        draft = upload(client, name="没跑.wav").json()          # 停在 draft，没有产物

        response = client.get("/api/exports/batch",
                              params={"ids": f"{done['id']},{draft['id']}"})
        zf = batch_zip(response)
        assert "有谱.svp" in zf.namelist()
        assert "没跑.svp" not in zf.namelist()
        skipped = manifest_of(zf)["skipped"]
        assert [item["name"] for item in skipped] == ["没跑"]
        assert "还没有" in skipped[0]["reason"]                  # "还没有产物可导出"


def test_batch_export_404_for_unknown_id(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = transcribed(client, "甲.wav")
        response = client.get("/api/exports/batch",
                              params={"ids": f"{job['id']},不存在的工程"})
        assert response.status_code == 404
        assert "不存在的工程" in response.json()["detail"]


def test_batch_export_409_when_nothing_to_export(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        draft = upload(client, name="没跑.wav").json()
        response = client.get("/api/exports/batch", params={"ids": draft["id"]})
        assert response.status_code == 409
        assert "没有" in response.json()["detail"]


def test_batch_export_does_not_overwrite_same_names(tmp_path):
    pytest.importorskip("libresvip", reason="可选依赖：pip install -e .[svp]")
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        first = transcribed(client, "同名.wav")
        second = transcribed(client, "同名.wav")
        assert first["name"] == second["name"] == "同名"

        zf = batch_zip(client.get("/api/exports/batch",
                                  params={"ids": f"{first['id']},{second['id']}"}))
        names = set(zf.namelist())
        assert {"同名.svp", "同名-2.svp"} <= names


def test_batch_export_rejects_unknown_parts(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = transcribed(client, "甲.wav")
        response = client.get("/api/exports/batch",
                              params={"ids": job["id"], "parts": "vocal,drums"})
        assert response.status_code == 400
        assert "drums" in response.json()["detail"]


def test_batch_export_rejects_empty_ids(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        response = client.get("/api/exports/batch", params={"ids": " , "})
        assert response.status_code == 400


def test_batch_export_files_kind_lays_out_per_job(tmp_path):
    """`kind=files`：产物按 `<歌名>/<相对路径>` 摊开，不套一层内层 zip。"""
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = transcribed(client, "甲.wav")
        zf = batch_zip(client.get("/api/exports/batch",
                                  params={"ids": job["id"], "kind": "files"}))
        names = set(zf.namelist())
        assert "甲/project.json" in names
        assert "甲/summary.json" in names
        assert "_manifest.json" in names
        # 内部元数据不进包（job.json / .webui/ 是 WebUI 自己的东西）
        assert not any(name.startswith("甲/job.json") for name in names)
        assert not any(".webui/" in name for name in names)
        assert manifest_of(zf)["kind"] == "files"


def test_batch_export_manifest_keeps_notes_on_cached_svp(tmp_path):
    """第二次导出命中缓存，清单里仍然要有音符数（统计旁文件），否则同一批两次下载对不上。"""
    pytest.importorskip("libresvip", reason="可选依赖：pip install -e .[svp]")
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = transcribed(client, "甲.wav")
        first = manifest_of(batch_zip(client.get("/api/exports/batch",
                                                 params={"ids": job["id"]})))
        second = manifest_of(batch_zip(client.get("/api/exports/batch",
                                                  params={"ids": job["id"]})))
        assert first["exported"][0]["notes"] > 0
        assert second["exported"][0]["notes"] == first["exported"][0]["notes"]


def test_batch_export_still_works_when_cached_svp_has_no_stats(tmp_path):
    """缓存命中但旁文件丢了（老缓存 / 写盘失败）也要能导出——不能因为"没有统计"就当没导。"""
    pytest.importorskip("libresvip", reason="可选依赖：pip install -e .[svp]")
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = transcribed(client, "甲.wav")
        batch_zip(client.get("/api/exports/batch", params={"ids": job["id"]}))
        for sidecar in (job_internal(manager, job) / "export").glob("*.svp.json"):
            sidecar.unlink()

        zf = batch_zip(client.get("/api/exports/batch", params={"ids": job["id"]}))
        assert "甲.svp" in zf.namelist()
        manifest = manifest_of(zf)
        assert [item["name"] for item in manifest["exported"]] == ["甲"]
        assert "notes" not in manifest["exported"][0]

