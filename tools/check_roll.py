"""钢琴卷帘的后端验收脚本（③ 换成卷帘后的回归测试）。

用法（在包根目录）：``python tools/check_roll.py``

为什么用桩：本机沙箱可能读不到工作区外的 site-packages，所以 fastapi 与 app.tasks
（会拉起 app.worker）都注入假模块。被验证的是**本项目自己的逻辑**。

覆盖：
  GET  /api/tasks/{id}/roll   导入（ABC）→ 轨/声部标签/小节表/拍号/各类告警
  POST /api/tasks/{id}/roll   保存 → roll.json → 同一条导出管线 → SVP 里真能看到歌词
  校验                         非法音符必须被 400 挡住，而不是一路走到 SVP/MIDI 写出
"""
from __future__ import annotations

import json
import shutil
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        FAILS.append(msg)


# --------------------------------------------------------------- fastapi 桩
class HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class JSONResponse:
    def __init__(self, content=None, status_code=200, **kw):
        self.payload = content
        self.status_code = status_code


def _install_stubs():
    fa = types.ModuleType("fastapi")

    class Body:
        def __init__(self, default=None, **kw):
            self.default = default

    class File:
        def __init__(self, default=None, **kw):
            self.default = default

    class Request:
        pass

    class UploadFile:
        pass

    class FastAPI:
        def __init__(self, **kw):
            pass

        def get(self, *a, **k):
            return lambda f: f

        def post(self, *a, **k):
            return lambda f: f

        def mount(self, *a, **k):
            pass

    fa.FastAPI = FastAPI
    fa.HTTPException = HTTPException
    fa.Body = Body
    fa.File = File
    fa.Request = Request
    fa.UploadFile = UploadFile
    sys.modules["fastapi"] = fa

    res = types.ModuleType("fastapi.responses")
    res.JSONResponse = JSONResponse
    res.FileResponse = JSONResponse
    res.StreamingResponse = JSONResponse
    sys.modules["fastapi.responses"] = res

    sf = types.ModuleType("fastapi.staticfiles")

    class StaticFiles:
        def __init__(self, **kw):
            pass

    sf.StaticFiles = StaticFiles
    sys.modules["fastapi.staticfiles"] = sf

    # 假的任务管理器：避开 app.worker（它会拉起整条推理链）
    class TaskBusyError(Exception):
        pass

    class _Manager:
        task = None
        busy_flag = False

        def get(self, tid):
            return self.task if tid == "t1" else None

        def busy(self):
            return self.busy_flag

        def broadcast_result(self, *a, **k):
            pass

    tk = types.ModuleType("app.tasks")
    tk.manager = _Manager()
    tk.TaskBusyError = TaskBusyError
    sys.modules["app.tasks"] = tk
    return tk.manager


MANAGER = _install_stubs()

from app.server import get_roll, pianoroll_js, save_roll  # noqa: E402
from app import rebuild as R  # noqa: E402

# ------------------------------------------------------------------ 测试数据
# 模型真实产出的 ABC 形状（见 app/abcfilter.py 顶部）：声部**声明行**带 clef=/name=，
# 正文里用裸 `V: Vocal` 切换。这个区别要紧：abcp 只把带 `=` 的声明行记进 score.voices，
# 裸切换行不记；一条声明都没有时会退化成「按声部名字母序」（Ins 会排到 Vocal 前面）。
ABC = """X:1
T:
M:4/4
L:1/16
Q:1/4=120
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
V: Ins clef=treble name="Ins Melody" snm="Inst."
K:C
V: Vocal
C2D2E2F2 G2A2B2c2 |
V: Ins
C,4E,4 G,4c4 |
"""

# 120 BPM / 4/4 → 每小节 2 秒；记谱位置以全音符计，一小节 = 1.0
MEASURES = [
    {"start": 0.0, "end": 2.0, "score_start": 0.0, "score_end": 1.0},
    {"start": 2.0, "end": 4.0, "score_start": 1.0, "score_end": 2.0},
]

tmp = ROOT / "_rollcheck_tmp"
if tmp.exists():
    shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir(parents=True, exist_ok=True)
print("[tmp] 使用包根下的临时目录 _rollcheck_tmp")
(tmp / "score.melody.abc").write_text(ABC, encoding="utf-8")
(tmp / "playback.json").write_text(json.dumps({"measures": MEASURES}), encoding="utf-8")


class Task:
    out_dir = str(tmp)
    audio = types.SimpleNamespace(stem="demo")
    params = {"export_voices": ["vocal", "ins"]}
    result = {"bpm": 120.0}


MANAGER.task = Task()

print("\n[1] GET /roll：从模型产出的 ABC 导入")
r = get_roll("t1", export_voices="vocal,ins")
p = r.payload
check(r.status_code == 200, "HTTP 200")
check(p["available"] is True, "available=True")
names = [t["voice"] for t in p["tracks"]]
kinds = [t["kind"] for t in p["tracks"]]
check(names == ["Vocal", "Ins"], f"轨顺序与 ABC 声明一致：{names}")
check(kinds == ["vocal", "ins"], f"声部归类正确：{kinds}")
check([t["display"] for t in p["tracks"]] == ["主人声", "器乐旋律"],
      f"中文显示名：{[t['display'] for t in p['tracks']]}")
check([t["is_vocal"] for t in p["tracks"]] == [True, False], "is_vocal 标记正确")
check(all(t["editable"] for t in p["tracks"]), "ABC 声部均可编辑")
check(p["source_abc"] == "score.melody.abc", f"记录导入来源：{p['source_abc']}")

allnotes = [n for t in p["tracks"] for n in t["notes"]]
check(len(allnotes) == 12, f"音符总数 = 12（实际 {len(allnotes)}）")
check(all(n["lyric"] == "" for n in allnotes), "初始没有歌词（空串，导出时落回 la）")
check(all(isinstance(n["pitch"], int) and 21 <= n["pitch"] <= 108 for n in allnotes),
      "音高都是合法 MIDI 整数")
check(all(n["start"] < n["end"] for n in allnotes), "没有零长/负长音符")

print("\n[2] 小节表 / 拍号 / 速度表 / 时长")
check(p["beats_per_bar"] == 4 and p["beat_unit"] == 4,
      f"拍号解析：{p['beats_per_bar']}/{p['beat_unit']}")
check(abs(float(p["unit_whole"]) - 0.0625) < 1e-9,
      f"ABC 默认音符长度 L:1/16 → unit_whole={p['unit_whole']}")
ms = p["measures"]
check(len(ms) == 2 and ms[0]["score_start"] == 0.0 and abs(ms[-1]["end"] - 4.0) < 1e-9,
      "小节表完整（网格与小节号要按它画，不能拿拍号硬乘）")
check(isinstance(p["tempo_map"], list) and p["tempo_map"][0]["t"] == 0.0,
      f"tempo_map 首项 t=0：{p['tempo_map']}")
check(abs(p["duration"] - 2.0) < 0.05, f"duration = 最大结束秒（{p['duration']}）")

print("\n[3] 勾选过滤")
p2 = get_roll("t1", only_melody=1).payload
check([t["voice"] for t in p2["tracks"]] == ["Vocal"], "只勾人声 → 只剩 Vocal 一条轨")
check(p2["export_voices"] == ["vocal"], f"export_voices 回显：{p2['export_voices']}")
p3 = get_roll("t1", export_voices="chords").payload
check(p3["available"] is False and p3["tracks"] == [],
      "勾了和弦但没有 chords.mid → available=False（不会假装成功）")
check(any("和弦" in w for w in p3["warnings"]), f"warnings 点明缺什么：{p3['warnings']}")

print("\n[4] 边界：任务不存在 / 还没有乐谱")
try:
    get_roll("nope")
    check(False, "不存在的任务应抛 404")
except HTTPException as e:
    check(e.status_code == 404, f"不存在的任务抛 404（{e.detail}）")
empty = tmp / "empty"
empty.mkdir()
Task.out_dir = str(empty)
p4 = get_roll("t1").payload
check(p4["available"] is False and any("扒谱" in w for w in p4["warnings"]),
      f"无乐谱时给出可执行的提示：{p4['warnings']}")
Task.out_dir = str(tmp)

print("\n[5] POST /roll：保存音符与歌词 → 重生成导出")
vocal = [t for t in get_roll("t1", export_voices="vocal").payload["tracks"]][0]
check(len(vocal["notes"]) == 8, f"人声轨 8 个音符（实际 {len(vocal['notes'])}）")
lyrics = ["我", "爱", "你", "窗", "外", "的", "麻", "雀"]
for n, ly in zip(vocal["notes"], lyrics):
    n["lyric"] = ly
# 顺手改一个音高，验证导出用的是卷帘的值
vocal["notes"][0]["pitch"] = 65

resp = save_roll("t1", {"tracks": [vocal], "export_voices": ["vocal"]})
j = resp.payload
check(resp.status_code == 200 and j.get("ok"), f"保存成功（status={resp.status_code}）")
exports = {e["kind"]: e for e in j.get("exports", [])}
check("svp" in exports, f"生成了 SVP：{list(exports)}")
check("abc" not in exports, "不再产出 ABC 产物（ABC 已从用户视野删除）")
check(j.get("lyrics_source") == "per_note", f"歌词来源标记为逐音：{j.get('lyrics_source')}")

roll_file = tmp / R.ROLL_FILENAME
check(roll_file.is_file(), f"落盘 {R.ROLL_FILENAME}")
saved = json.loads(roll_file.read_text(encoding="utf-8"))
saved_voices = [t["voice"] for t in saved["tracks"]]
check("Ins" in saved_voices,
      f"只保存了人声轨，器乐轨仍留在存档里（按声部名合并）：{saved_voices}")

print("\n[6] 导出的 SVP 里真的能看到卷帘的歌词与音高")
svp_path = Path(exports["svp"]["path"])
check(svp_path.is_file(), f"SVP 文件存在：{svp_path.name}")
proj = json.loads(svp_path.read_text(encoding="utf-8"))
notes0 = proj["tracks"][0]["mainGroup"]["notes"]
check(len(notes0) == 8, f"SVP 第 1 轨 8 个音符（实际 {len(notes0)}）")
got = [n["lyrics"] for n in notes0]
check(got == lyrics, f"SVP 里的歌词就是卷帘填的：{got}")
check(notes0[0]["pitch"] == 65, f"音高用的是卷帘改后的值（{notes0[0]['pitch']}）")
check((exports["svp"].get("tracks") or []) == ["主人声"],
      f"只导出被勾选的声部：{exports['svp'].get('tracks')}")

print("\n[7] 再次读取：拿到的是保存后的内容（不是模型原始 ABC）")
p5 = get_roll("t1", export_voices="vocal").payload
check([n["lyric"] for n in p5["tracks"][0]["notes"]] == lyrics,
      "重新打开卷帘仍是填好的歌词")
check(p5["tracks"][0]["notes"][0]["pitch"] == 65, "改过的音高也还在")
p6 = get_roll("t1", export_voices="vocal,ins").payload
check([t["voice"] for t in p6["tracks"]] == ["Vocal", "Ins"],
      f"把器乐勾回来，它没被丢掉：{[t['voice'] for t in p6['tracks']]}")
ins_notes = [t for t in p6["tracks"] if t["voice"] == "Ins"][0]["notes"]
check(all(n["lyric"] == "" for n in ins_notes), "器乐轨仍是无歌词状态")

print("\n[8] 同轨重叠：导出侧按既有策略拆成多条单音轨（信息不丢）")
both = [t for t in get_roll("t1", export_voices="vocal,ins").payload["tracks"]]
v = [t for t in both if t["voice"] == "Vocal"][0]
# 把第 2 个音符往前拖成与前一个重叠
v["notes"][1]["start"] = v["notes"][0]["start"] + 0.05
j8 = save_roll("t1", {"tracks": [v], "export_voices": ["vocal"]}).payload
check(j8.get("ok"), "重叠数据仍能保存")
check(any("拆轨" in w for w in j8.get("warnings", [])),
      f"导出把重叠轨拆成多条并写进 warnings：{[w for w in j8.get('warnings', []) if '拆轨' in w]}")
tracks8 = j8["exports"][0]["tracks"] if j8.get("exports") else []
check(len(tracks8) >= 2, f"确实拆成了多条单音轨：{tracks8}")

print("\n[9] 非法音符必须被挡住（不能一路走到 SVP/MIDI 写出）")
def bad(notes, why):
    t = {"voice": "Vocal", "kind": "vocal", "notes": notes}
    try:
        save_roll("t1", {"tracks": [t], "export_voices": ["vocal"]})
        check(False, f"{why} 应被拒绝")
    except HTTPException as e:
        check(e.status_code == 400, f"{why} → 400（{e.detail}）")

bad([{"start": -1.0, "end": 0.5, "pitch": 60, "lyric": "x"}], "起点为负")
bad([{"start": 0.5, "end": 0.5, "pitch": 60, "lyric": "x"}], "零长音符")
bad([{"start": 0.0, "end": 0.5, "pitch": 200, "lyric": "x"}], "音高越界")
bad([{"start": 0.0, "end": 0.5, "lyric": "x"}], "缺 pitch")

print("\n[10] 和弦轨（只读）不会被写进存档")
chordish = {"voice": "chords", "kind": "chords", "editable": False,
            "notes": [{"start": 0.0, "end": 1.0, "pitch": 60, "lyric": ""}]}
save_roll("t1", {"tracks": [chordish], "export_voices": ["vocal"]})
saved2 = json.loads(roll_file.read_text(encoding="utf-8"))
check("chords" not in [t["voice"] for t in saved2["tracks"]],
      f"和弦轨没有被落盘：{[t['voice'] for t in saved2['tracks']]}")

print("\n[11] 卷帘组件的静态路由")
try:
    resp = pianoroll_js()
    served = str(getattr(resp, "payload", ""))
    check("pianoroll.js" in served.replace("\\", "/"), f"路由指向 web/pianoroll.js")
    size = (ROOT / "web" / "pianoroll.js").stat().st_size
    check(size > 20000, f"pianoroll.js 已就位（{size} 字节）")
except Exception as e:  # noqa: BLE001
    check(False, f"取卷帘组件失败：{e}")

print("\n[12] 旧的 lyrics.json 只提示、不再被消费")
(tmp / "lyrics.json").write_text('{"words":[{"text":"以","start":0,"end":0.3}]}',
                                 encoding="utf-8")
p7 = get_roll("t1", export_voices="vocal").payload
check(any("Ctrl+L" in w for w in p7["warnings"]),
      f"提示旧歌词不再参与导出：{[w for w in p7['warnings'] if 'Ctrl+L' in w]}")
check([n["lyric"] for n in p7["tracks"][0]["notes"]] == lyrics,
      "旧 lyrics.json 没有覆盖卷帘里的歌词")

print("\n[13] ④ 的 LRC 机器确已清掉，但留下的对齐能力仍可用")
import app.lyrics as L  # noqa: E402

gone = [n for n in ("parse_lrc", "looks_like_lrc", "words_from_lrc", "words_from_plain",
                    "fill_lyrics", "build_lines", "build_lrc", "SECTION_LABELS")
        if hasattr(L, n)]
check(not gone, f"LRC 相关 API 已全部删除（残留：{gone}）")
kept = [n for n in ("LyricWord", "assign_lyrics", "continuation_mark") if hasattr(L, n)]
check(len(kept) == 3, f"rebuild 需要的三个名字还在：{kept}")
words = [L.LyricWord("我", 0.0, 0.5), L.LyricWord("爱", 0.5, 1.0), L.LyricWord("你", 1.0, 1.6)]
lys, st = L.assign_lyrics(words, [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0)])
check(lys == ["我", "爱", "你", "la"], f"对齐仍正确（多出的音符落回占位词）：{lys}")
check(st["matched"] == 3 and st["notes"] == 4, f"统计仍正确：{st['matched']}/{st['notes']}")

print("\n[21] 导入来源：必须读**未过滤**的全量乐谱（用户实测踩坑）")
# 真实数据的样子：score.abc 有 Vocal+Ins，score.melody.abc 是推理时按默认勾选
# （只勾人声）过滤过的、**只剩 Vocal**。早期实现先读 score.melody.abc，于是
# "器乐声部凭空消失"：勾了器乐也只有 1 条轨，取消勾选人声就变成"没有可用音符"。
filtered_dir = tmp / "filtered"
filtered_dir.mkdir()
# 过滤版：只留 Vocal
(filtered_dir / "score.melody.abc").write_text(
    ABC.split("V: Ins clef=treble")[0].split("V: Vocal clef=treble")[0]
    + 'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"\n'
    + "K:C\nV: Vocal\nC2D2E2F2 G2A2B2c2 |\n",
    encoding="utf-8",
)
# 全量版：两个声部都在
(filtered_dir / "score.abc").write_text(ABC, encoding="utf-8")
(filtered_dir / "playback.json").write_text(json.dumps({"measures": MEASURES}), encoding="utf-8")
Task.out_dir = str(filtered_dir)

p8 = get_roll("t1", export_voices="vocal,ins").payload
check(p8["source_abc"] == "score.abc", f"导入来源是全量乐谱（实际 {p8['source_abc']}）")
check([t["voice"] for t in p8["tracks"]] == ["Vocal", "Ins"],
      f"两个声部都在，没有被过滤版吃掉：{[t['voice'] for t in p8['tracks']]}")
check(p8["voices_in_score"] == ["Ins", "Vocal"], f"回报名单：{p8['voices_in_score']}")

p9 = get_roll("t1", export_voices="ins").payload
check(p9["available"] is True, "只勾器乐旋律也有内容（以前这里是空的）")
check([t["voice"] for t in p9["tracks"]] == ["Ins"],
      f"只勾器乐 → 只剩乐器轨：{[t['voice'] for t in p9['tracks']]}")
check(len([n for t in p9["tracks"] for n in t["notes"]]) == 4, "器乐轨 4 个音符")

print("\n[22] 选了乐谱里没有的声部：原因要说清有哪些声部")
# 用一个干净的目录：tmp 里已经有前面保存的 roll.json（含两个声部），拿它测不出这条。
vocalonly = tmp / "vocalonly"
vocalonly.mkdir()
(vocalonly / "score.abc").write_text(
    ABC.split("V: Ins clef=treble")[0].split("V: Vocal clef=treble")[0]
    + 'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"\n'
    + "K:C\nV: Vocal\nC2D2E2F2 G2A2B2c2 |\n",
    encoding="utf-8",
)
(vocalonly / "playback.json").write_text(json.dumps({"measures": MEASURES}), encoding="utf-8")
Task.out_dir = str(vocalonly)

p10 = get_roll("t1", export_voices="ins").payload
check(p10["available"] is False, "这首歌只有 Vocal，只勾器乐 → available=False")
check("Vocal" in p10.get("reason", ""),
      f"原因里点明乐谱实际有哪些声部：{p10.get('reason')}")
check(p10.get("voices_in_score") == ["Vocal"], f"并带上机器可读的名单：{p10.get('voices_in_score')}")
check(any("ins" in w for w in p10["warnings"]), f"同时给出告警：{p10['warnings']}")

p11 = get_roll("t1", export_voices="vocal").payload
check(p11["available"] is True, "改回勾人声就正常了（用户能自己救回来）")
Task.out_dir = str(tmp)

print("\n[23] 存档自愈：旧版本（读过滤版乐谱）存下的 roll.json 缺声部，要补回来")
# 场景：用户在旧版本下保存过一次 → roll.json 里只有 Vocal；现在乐谱里有 Vocal+Ins。
# 卷帘没有"删除整个声部"的功能，所以缺声部只可能是那个 bug 造成的，应当补回。
heal = tmp / "heal"
heal.mkdir()
(heal / "score.abc").write_text(ABC, encoding="utf-8")
(heal / "playback.json").write_text(json.dumps({"measures": MEASURES}), encoding="utf-8")
vocal_only = [t for t in get_roll("t1", export_voices="vocal").payload["tracks"]
              if t["voice"] == "Vocal"][0]
Task.out_dir = str(heal)
(heal / R.ROLL_FILENAME).write_text(json.dumps({
    "version": 1, "source_abc": "score.melody.abc", "bpm": 120.0,
    "header": {"q": 120.0, "meter": "4/4", "meter_tuple": [4, 4], "unit_whole": 0.0625},
    "tracks": [dict(vocal_only)],
}, ensure_ascii=False), encoding="utf-8")

p12 = get_roll("t1", export_voices="vocal,ins").payload
check([t["voice"] for t in p12["tracks"]] == ["Vocal", "Ins"],
      f"存档里缺的器乐声部被补回来了：{[t['voice'] for t in p12['tracks']]}")
check(len([n for t in p12["tracks"] for n in t["notes"]]) == 12,
      "补回来的是乐谱里的音符（8 人声 + 4 器乐）")
p13 = get_roll("t1", export_voices="ins").payload
check(p13["available"] is True and [t["voice"] for t in p13["tracks"]] == ["Ins"],
      "只勾器乐也能拿到内容")
# 存档里已有的声部要以**存档**为准（用户改过的音符不能被乐谱覆盖）
saved_first = p12["tracks"][0]["notes"][0]["pitch"]
check(saved_first == vocal_only["notes"][0]["pitch"], "存档里已有的声部仍以存档为准")
Task.out_dir = str(tmp)

# ------------------------------------------------------------------ 收尾
shutil.rmtree(tmp, ignore_errors=True)
print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：{FAILS}"))
sys.exit(1 if FAILS else 0)
