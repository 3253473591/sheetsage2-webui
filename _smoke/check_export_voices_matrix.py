"""导出内容矩阵测试：**每一种勾选组合**导出的轨道必须**恰好**是勾的那些。

不需要跑推理：拿一个已有真实任务目录（含 score.abc + chords.mid）复制到 tmp\，
对每种组合调 rebuild_exports()，再**把导出的 SVP 解析回来**数轨道名。

为什么要有这个：和弦轨来自 chords.mid、**不是 ABC 声部**，走的是另一条分支
（`chord_notes = load_chord_notes(out) if "chords" in sel else []`）。
只勾「器乐旋律」时如果和弦跟着出来，就是在这一条分支上漏了判断——
而这种错误在界面上表现为"我只勾了器乐，怎么多出一条和弦轨"。

运行：  .\\.venv\\Scripts\\python.exe _smoke\\check_export_voices_matrix.py
"""
import itertools
import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.rebuild import EXPORT_VOICE_TOKENS, rebuild_exports   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


def pick_sample():
    """找一个同时有 score.abc 和 chords.mid 的真实任务目录。"""
    for d in sorted(ROOT.glob("output/*"), reverse=True):
        if (d / "score.abc").is_file() and (d / "chords.mid").is_file():
            return d
    return None


def svp_track_names(path: pathlib.Path):
    data = json.loads(path.read_text(encoding="utf-8").rstrip("\x00 \r\n\t"))
    return [t.get("name") for t in (data.get("tracks") or [])]


sample = pick_sample()
if sample is None:
    print("找不到同时含 score.abc 与 chords.mid 的样本 —— 跳过")
    sys.exit(0)
print(f"样本：{sample.name}")
_existing = sorted((sample / "export").glob("*.svp")) if (sample / "export").is_dir() else []
if _existing:
    print(f"该样本现有产物轨道：{svp_track_names(_existing[0])}")
else:
    print("该样本暂时没有 export/*.svp（下面会现做）")
print()

work = ROOT / "tmp" / "voice_matrix"
expect_chord = "和弦"
expect_vocal = "主人声"
expect_ins = "器乐旋律"

for r in range(1, len(EXPORT_VOICE_TOKENS) + 1):
    for combo in itertools.combinations(EXPORT_VOICE_TOKENS, r):
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        # 只复制 rebuild_exports 需要的东西：ABC、小节表、和弦
        for f in ("score.abc", "playback.json", "chords.mid", "summary.json", "lyrics.json"):
            src = sample / f
            if src.is_file():
                shutil.copy2(src, work / f)

        info = rebuild_exports(
            work,
            (work / "score.abc").read_text(encoding="utf-8"),
            export_voices=list(combo),
            project_name="matrix",
        )
        if not info.get("ok"):
            check(f"组合 {list(combo)} 能导出", False, str(info.get("error")))
            continue

        svp = work / "export" / "matrix.svp"
        if not svp.is_file():
            check(f"组合 {list(combo)} 产出 SVP", False, "没有 matrix.svp")
            continue
        names = svp_track_names(svp)
        joined = " / ".join(str(n) for n in names)

        want_vocal = "vocal" in combo
        want_ins = "ins" in combo
        want_chords = "chords" in combo

        has_vocal = any(str(n).startswith(expect_vocal) for n in names)
        has_ins = any(str(n).startswith(expect_ins) for n in names)
        has_chord = any(str(n).startswith(expect_chord) for n in names)

        ok = (has_vocal == want_vocal and has_ins == want_ins and has_chord == want_chords)
        check(f"{'+'.join(combo):<18} → [{joined}]", ok,
              f"期望 人声={want_vocal} 器乐={want_ins} 和弦={want_chords}，"
              f"实际 人声={has_vocal} 器乐={has_ins} 和弦={has_chord}")

        # 顺序也必须是**规范顺序**（人声 → 器乐 → 和弦）。
        # 为什么专门断言这个：轨道顺序以前跟着 ABC 里 V: 的声明顺序走，而落到哪个
        # ABC 文件取决于编辑稿/模型原始稿哪份"能满足勾选"，于是**同一份内容两次导出
        # 可能给出不同的轨道顺序**（出厂验收实测踩到：主人声/器乐旋律 反了）。
        _rank = {"主人声": 0, "器乐旋律": 1, "和弦": 2}

        def _rank_of(n):
            for pre, r in _rank.items():
                if str(n).startswith(pre):
                    return r
            return 9

        _seq = [_rank_of(n) for n in names]
        check(f"{'+'.join(combo):<18} 轨道顺序规范", _seq == sorted(_seq),
              f"顺序 {names} → rank {_seq}")

        # 顺带核对 MIDI 侧（同一套 sel，不该各说各话）
        mid = work / "export" / "matrix.mid"
        if mid.is_file():
            import mido
            mnames = []
            for tr in mido.MidiFile(str(mid)).tracks:
                for msg in tr:
                    if msg.type == "track_name":
                        mnames.append(msg.name)
                        break
            mid_joined = " / ".join(mnames)
            # MIDI 轨名走 Latin-1，所以用 ABC 里的 **ASCII 声部名**（Vocal / Ins / ChordsN），
            # 不是界面显示名（主人声 / 器乐旋律 / 和弦）。这里断言的是同一套语义。
            m_ok = (any(n.startswith("Vocal") for n in mnames) == want_vocal
                    and any(n.startswith("Ins") for n in mnames) == want_ins
                    and any(n.startswith("Chords") for n in mnames) == want_chords)
            check(f"{'+'.join(combo):<18} MIDI [{mid_joined}]", m_ok,
                  f"MIDI 与 SVP 不一致；SVP=[{joined}]")

shutil.rmtree(work, ignore_errors=True)
print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 每种勾选组合导出的轨道恰好是勾的那些（SVP 与 MIDI 一致）")
