"""钢琴卷帘保存（/api/tasks/{id}/roll）的端到端测试。

跑一个真任务，然后像前端那样保存卷帘，并**从导出的 SVP 里读回来**核对：

  1. 只改歌词 → 导出里的歌词真的变成填的那些，且 ``-`` / ``+`` 是**字面保留**的；
  2. 改音高 → 导出里的音高真的跟着变（证明编辑真的影响产物，而不是只改了界面）；
  3. 同一轨里造一个重叠 → 后端自动拆成两条并给出 warning（Synthesizer V 一轨一音）；
  4. `has_vocal_lyrics` 先 false 后 true —— 导出前的"未填歌词"确认框就靠它；
  5. 空 tracks → 400，不写坏任何东西。

运行（会自己起服务；需要一首音频）：
  .\\.venv\\Scripts\\python.exe _smoke\\check_roll_save.py [音频]
"""
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 这个脚本主体是打 HTTP，但它开头有一段**不依赖服务**的单元检查要 import app.*，
# 所以必须把项目根加进来（其他 _smoke 脚本都加了，这个一开始漏了）。
sys.path.insert(0, str(ROOT))
AUDIO = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tmp" / "duet_in" / "duet.flac"
PORT = 8803
URL = f"http://127.0.0.1:{PORT}/"
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


def post(path, obj, timeout=1800):
    req = urllib.request.Request(
        URL + path, data=json.dumps(obj).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, {}


def get(path, timeout=600):
    with urllib.request.urlopen(URL + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def svp_notes():
    """从导出的 SVP 里读回每轨的音符（这是**产物真相**，不是记账）。"""
    with urllib.request.urlopen(URL + f"api/tasks/{TASK}/export/svp", timeout=300) as r:
        data = json.loads(r.read().decode("utf-8").rstrip("\x00 \r\n\t"))
    out = []
    for t in data.get("tracks") or []:
        notes = [
            {"onset": n.get("onset"), "duration": n.get("duration"),
             "pitch": n.get("pitch"), "lyrics": n.get("lyrics")}
            for n in (t.get("mainGroup") or {}).get("notes") or []
        ]
        out.append((t.get("name"), notes))
    return out


if not AUDIO.is_file():
    print(f"找不到测试音频：{AUDIO}")
    sys.exit(2)


# ==========================================================================
#  第 0 段：不依赖服务的单元检查 —— "谱面回落" 到底会不会发生
# ==========================================================================
# 为什么单独测这个：端到端那条任务里 export/<歌名>.abc 恰好两条声部都有，
# 所以**回落根本不会触发**，光靠端到端测不到这条分支（第一次就是这么漏的）。
# 这里造一个"只导出了人声"的任务目录，直接验 _score_for 的行为。
def _unit_score_fallback():
    import shutil
    import tempfile
    from types import SimpleNamespace

    from app.server import _score_for

    VOCAL_ONLY = "X:1\nT:t\nM:4/4\nL:1/8\nQ:1/4=120\nK:C\nV:Vocal\nC D E F |\n"
    BOTH = VOCAL_ONLY + "V:Ins\nC, D, E, F, |\n"

    work = pathlib.Path(tempfile.mkdtemp(prefix="dsh-rollscore-"))
    try:
        (work / "export").mkdir(parents=True, exist_ok=True)
        # 模拟"任务是用只勾人声跑的"：编辑稿与导出稿都只有 Vocal，
        # 只有模型原始 score.abc 才有两条旋律
        (work / "export" / "song.abc").write_text(VOCAL_ONLY, encoding="utf-8")
        (work / "score.melody.abc").write_text(VOCAL_ONLY, encoding="utf-8")
        (work / "score.abc").write_text(BOTH, encoding="utf-8")
        task = SimpleNamespace(audio=SimpleNamespace(stem="song"))

        p, _sc, fell = _score_for(work, task, {"ins"})
        check("回落：要器乐但编辑稿只有人声 → 用模型原始 score.abc",
              p is not None and p.name == "score.abc", f"{p.name if p else None}")
        check("回落：并明确标出 fell_back=True（不静默换数据）", fell is True, str(fell))

        p2, _sc2, fell2 = _score_for(work, task, {"vocal"})
        check("不回落：要人声且编辑稿就有 → 用编辑稿 export/song.abc",
              p2 is not None and p2.name == "song.abc", f"{p2.name if p2 else None}")
        check("不回落：fell_back=False", fell2 is False, str(fell2))

        p3, _sc3, fell3 = _score_for(work, task, {"chords"})
        check("只要和弦时不因为 ABC 缺声部而回落（和弦本来就不在 ABC 里）",
              p3 is not None and fell3 is False, f"{p3.name if p3 else None} fell={fell3}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


_unit_score_fallback()
print()

proc = subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "app.server",
                         "--no-browser", "--port", str(PORT)],
                        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
TASK = None
try:
    for _ in range(120):
        try:
            urllib.request.urlopen(URL + "api/health", timeout=3)
            break
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    print(f"服务就绪 {URL}")

    # ---- 上传 + 跑一个短任务（默认只勾人声主旋律）----
    boundary = "----roll" + uuid.uuid4().hex
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{AUDIO.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + AUDIO.read_bytes() + \
           f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(URL + "api/upload", data=body, method="POST",
                                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    token = json.loads(urllib.request.urlopen(req, timeout=900).read())["token"]

    st, j = post("api/tasks", {"token": token, "export_voices": ["vocal"],
                               "max_seconds": 25, "preset": "default"})
    TASK = j.get("task_id")
    check("提交任务", st == 200 and bool(TASK), f"{st} {j}")
    deadline = time.time() + 900
    while time.time() < deadline:
        s = get("api/state")
        if (s.get("task") or {}).get("state") in ("done", "failed", "cancelled"):
            break
        time.sleep(3)
    state = (get("api/state").get("task") or {}).get("state")
    check("任务完成", state == "done", state)
    if state != "done":
        raise SystemExit(1)

    # ---- /notes 要给卷帘三样新东西 ----
    n0 = get(f"api/tasks/{TASK}/notes?only_melody=1")
    check("/notes 带 tracks", isinstance(n0.get("tracks"), list) and len(n0["tracks"]) >= 1,
          str(type(n0.get("tracks"))))
    check("/notes 带 tempo_map", isinstance(n0.get("tempo_map"), list) and n0["tempo_map"],
          str(n0.get("tempo_map")))
    check("/notes 带 has_vocal_lyrics", "has_vocal_lyrics" in n0, str(list(n0)))
    check("初始 has_vocal_lyrics=False（还没填歌词）", n0.get("has_vocal_lyrics") is False,
          str(n0.get("has_vocal_lyrics")))

    tracks = n0["tracks"]
    # ---- /notes 的勾选口径：**只勾器乐时不能带出和弦** ----
    # 这是用户报的 bug："勾选器乐旋律会出现和弦一起导出、预览"。
    # 导出侧一直是对的（见 check_export_voices_matrix.py），错的是 /notes 用
    # only_melody 那个**布尔**：它只有两个值，"只勾器乐"被当成"全都要"，
    # 于是和弦被一并塞进预览。现在 /notes 收显式的 voices。
    n_ins = get(f"api/tasks/{TASK}/notes?voices=ins")
    check("voices=ins → chord_notes 为空（勾器乐不该带出和弦）",
          (n_ins.get("chord_notes") or []) == [],
          f"{len(n_ins.get('chord_notes') or [])} 个和弦音")
    check("voices=ins → tracks 只有器乐",
          [t["voice"] for t in (n_ins.get("tracks") or [])] == ["Ins"],
          str([t["voice"] for t in (n_ins.get("tracks") or [])]))
    check("voices=ins → 回落标记是个布尔（不静默换数据）",
          isinstance(n_ins.get("fell_back_to_model"), bool),
          str(n_ins.get("fell_back_to_model")))
    check("voices=ins → 兼容字段 only_melody=False", n_ins.get("only_melody") is False,
          str(n_ins.get("only_melody")))

    n_v = get(f"api/tasks/{TASK}/notes?voices=vocal")
    check("voices=vocal → 只有人声、无和弦",
          [t["voice"] for t in (n_v.get("tracks") or [])] == ["Vocal"]
          and (n_v.get("chord_notes") or []) == [],
          str([t["voice"] for t in (n_v.get("tracks") or [])]))
    check("voices=vocal → 兼容字段 only_melody=True", n_v.get("only_melody") is True)

    n_ic = get(f"api/tasks/{TASK}/notes?voices=ins,chords")
    check("voices=ins,chords → 这时才带和弦",
          len(n_ic.get("chord_notes") or []) > 0,
          str(len(n_ic.get("chord_notes") or [])))

    n_none = get(f"api/tasks/{TASK}/notes?voices=")
    check("voices 为空 → 什么都不给（尊重三个勾选全取消）",
          (n_none.get("tracks") or []) == [] and (n_none.get("chord_notes") or []) == [],
          f"tracks={n_none.get('tracks')}")

    vocal = next((t for t in tracks if t.get("is_vocal")), tracks[0])
    notes = vocal["notes"]
    check(f"人声轨有音符（{len(notes)} 个）", len(notes) >= 3, str(len(notes)))
    pitches0 = [n["pitch"] for n in notes]

    # ---- 1. 只改歌词：- 与 + 必须字面保留 ----
    lyrics = ["wo", "+", "+", "-", "-", "ni"]
    payload = {"tracks": [{"voice": vocal["voice"],
                           "notes": [{"start": n["start"], "end": n["end"], "pitch": n["pitch"],
                                      "lyric": (lyrics[i] if i < len(lyrics) else "la")}
                                     for i, n in enumerate(notes)]}],
               "export_voices": ["vocal"], "bpm": n0.get("bpm")}
    st, j = post(f"api/tasks/{TASK}/roll", payload)
    check("保存卷帘（只改歌词）返回 200", st == 200 and j.get("ok"), f"{st} {j}")
    check("返回 has_vocal_lyrics=True", j.get("has_vocal_lyrics") is True, str(j.get("has_vocal_lyrics")))
    check("没有意外的拆轨 warning", not [w for w in (j.get("warnings") or []) if "重叠" in w],
          str(j.get("warnings")))

    tr = svp_notes()
    got = next((ns for name, ns in tr if name == "主人声"), None)
    check("SVP 里有「主人声」轨", got is not None, str([n for n, _ in tr]))
    if got:
        lyr = [x["lyrics"] for x in got]
        check("歌词写进了 SVP", lyr[:6] == lyrics, str(lyr[:8]))
        check("`-`（延音）字面保留，没有被解释掉", "-" in lyr, str(lyr[:8]))
        check("`+`（多音节）字面保留", "+" in lyr, str(lyr[:8]))
        check(f"音符数不变（{len(notes)}）", len(got) == len(notes), f"{len(notes)} → {len(got)}")

    # ---- 2. 改音高：导出必须跟着变 ----
    # 造一个"一定与原值不同"的目标音高，这样断言才有意义（不然改了个一样的就是假通过）
    new_pitch = {}
    for i in range(min(3, len(notes))):
        new_pitch[i] = pitches0[i] + 1 if pitches0[i] < 108 else pitches0[i] - 1
    payload2 = {"tracks": [{"voice": vocal["voice"],
                            "notes": [{"start": n["start"], "end": n["end"],
                                       "pitch": new_pitch.get(i, n["pitch"]),
                                       "lyric": (lyrics[i] if i < len(lyrics) else "la")}
                                      for i, n in enumerate(notes)]}],
                "export_voices": ["vocal"], "bpm": n0.get("bpm")}
    st, j = post(f"api/tasks/{TASK}/roll", payload2)
    check("保存卷帘（改音高）返回 200", st == 200 and j.get("ok"), f"{st} {j}")
    tr2 = svp_notes()
    got2 = next((ns for name, ns in tr2 if name == "主人声"), None) or []
    after = [x["pitch"] for x in got2][:3]
    want = [new_pitch[i] for i in range(len(new_pitch))]
    before = pitches0[:3]
    check(f"改过的音高出现在产物里（{before} → {after}，期望 {want}）",
          after == want and after != before, f"before={before} want={want} after={after}")

    # ---- 3. 造一个真重叠：应拆成两条 + 给 warning ----
    ov = list(notes[:2])
    if len(ov) >= 2:
        mid = (ov[0]["start"] + ov[0]["end"]) / 2
        payload3 = {"tracks": [{"voice": vocal["voice"], "notes": [
            {"start": ov[0]["start"], "end": ov[0]["end"], "pitch": ov[0]["pitch"], "lyric": "la"},
            {"start": mid, "end": ov[1]["end"], "pitch": ov[1]["pitch"] + 2, "lyric": "la"},
        ]}], "export_voices": ["vocal"], "bpm": n0.get("bpm")}
        st, j = post(f"api/tasks/{TASK}/roll", payload3)
        check("造重叠后仍能保存", st == 200 and j.get("ok"), f"{st} {j}")
        check("给出了重叠拆轨的 warning",
              any("重叠" in w for w in (j.get("warnings") or [])), str(j.get("warnings")))
        names = [v for v in (j.get("voices") or [])]
        # 拆出来的每一条都带序号（Vocal1 / Vocal2）—— 与导出侧 split_polyphonic_tracks 的
        # 命名惯例一致：只要拆了，就都有编号，不搞"第一条保留原名"的特例。
        check(f"声部被拆成两条且都带序号（{names}）",
              len(names) == 2 and all(n.startswith(vocal["voice"]) for n in names),
              str(names))
        check(f"拆出来的名字确实是 {vocal['voice']}1 / {vocal['voice']}2",
              names == [f"{vocal['voice']}1", f"{vocal['voice']}2"], str(names))

    # ---- 4. 空 tracks → 400 ----
    st, j = post(f"api/tasks/{TASK}/roll", {"tracks": [], "export_voices": ["vocal"]})
    check("空 tracks 返回 400（不写坏产物）", st == 400, f"{st} {j}")

finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 卷帘编辑真的影响导出产物：歌词（含 -/+）、音高、重叠拆轨、未填歌词标记")
