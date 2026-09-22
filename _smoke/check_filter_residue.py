"""回归：对 output/ 下所有 score.abc 跑后端 filter_abc(only_melody=True)，
确认「仅主旋律乐谱」过滤后没有任何 V: Ins 残留。

样本来自真实扒谱（output/<任务>/score.abc）；没有样本时 SKIP，不算失败。
    .venv\\Scripts\\python.exe _smoke\\check_filter_residue.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.abcfilter import filter_abc  # noqa: E402

root = pathlib.Path(__file__).resolve().parent.parent / "output"
files = sorted(root.rglob("score.abc")) if root.is_dir() else []
if not files:
    print("SKIP: output/ 下没有 score.abc（先随便扒一首歌再跑）。")
    sys.exit(0)

bad = 0
for p in files:
    text = p.read_text(encoding="utf-8")
    out, rep = filter_abc(text, only_melody=True)
    residue = [ln for ln in out.splitlines() if ln.strip().startswith("V:") and "Ins" in ln]
    if residue:
        bad += 1
        print("RESIDUE", p.parent.name, residue[:3])
    else:
        print("ok     ", p.parent.name, "kept:", rep.kept_voices, "dropped:", rep.dropped_voices)
print(f"scanned {len(files)} files, residue files: {bad}")
sys.exit(1 if bad else 0)
