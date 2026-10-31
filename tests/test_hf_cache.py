"""HF 缓存放哪、MERT 骨架从哪来 —— 两条都和"能不能脱网跑"直接相关。"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui import bootstrap as bs  # noqa: E402
from sheetsage2_webui import engine as en  # noqa: E402


def test_hf_cache_is_pinned_into_the_project(monkeypatch):
    monkeypatch.setenv("HF_HOME", r"D:\cache")
    monkeypatch.delenv("SHEETSAGE_KEEP_HF_HOME", raising=False)
    bs.ensure_hf_cache()
    expected = bs.huggingface_cache_dir()
    assert expected.is_relative_to(bs.PROJECT_ROOT)
    assert bs.os.environ["HF_HOME"] == str(expected)
    assert bs.os.environ["HF_MODULES_CACHE"] == str(expected / "modules")


def test_hf_cache_can_be_kept_if_the_user_insists(monkeypatch):
    monkeypatch.setenv("HF_HOME", r"D:\cache")
    monkeypatch.setenv("SHEETSAGE_KEEP_HF_HOME", "1")
    bs.ensure_hf_cache()
    assert bs.os.environ["HF_HOME"] == r"D:\cache"


def test_local_backbone_is_used_when_present(monkeypatch, tmp_path):
    models = tmp_path / "models"
    backbone = models / "MERT-v2-FullSong"
    backbone.mkdir(parents=True)
    (backbone / "model.safetensors").write_text("x", encoding="utf-8")
    monkeypatch.setattr(en, "PROJECT_ROOT", tmp_path)
    assert en.local_backbone_path() == backbone

    (backbone / "model.safetensors").unlink()
    assert en.local_backbone_path() is None
