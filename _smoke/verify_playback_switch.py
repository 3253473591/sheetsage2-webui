"""决定性验证：先播放第一首生成钢琴缓冲，再切到第二首，确认缓冲被作废并重建。"""
import time

from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8777/"
errs = []
with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", args=["--autoplay-policy=no-user-gesture-required"])
    pg = b.new_page(viewport={"width": 1360, "height": 1000})
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)
    pg.goto(B, wait_until="domcontentloaded")
    pg.wait_for_timeout(2000)

    # 找出服务端历史上两个不同的任务
    hist = pg.evaluate("""async () => {
      const s = await (await fetch('/api/state')).json();
      return {current: s.task && s.task.task_id, history: (s.history||[]).map(h=>h.task_id)};
    }""")
    print("当前任务:", hist["current"], "| 历史:", hist["history"][:5])
    ids = [i for i in [hist["current"]] + hist["history"] if i]
    uniq = list(dict.fromkeys(ids))
    if len(uniq) < 2:
        print("需要至少两个任务才能验证；请先跑两首")
        b.close()
        raise SystemExit(0)
    first, second = uniq[-1], uniq[0]   # 历史里最后一个是最早的
    print(f"任务A(早)={first}  任务B(新)={second}")

    def apply(tid):
        pg.evaluate("""async (tid) => {
          const s = await (await fetch(`/api/state?task_id=${tid}`)).json();
          applyTask(s.task, true);
        }""", tid)
        pg.wait_for_timeout(2500)

    def play_and_probe(tag):
        pg.evaluate("() => { document.getElementById('tpPlay').disabled=false; }")
        pg.click("#tpPlay")
        for _ in range(60):
            state = pg.evaluate("""() => ({
              piano: pianoData ? Math.round(pianoData.buffer.duration*1000)/1000 : null,
              notes: (S.playback&&S.playback.notes)?S.playback.notes.length:0,
              sig: S.notesSig,
            })""")
            if state["piano"]:
                break
            time.sleep(0.5)
        pg.wait_for_timeout(600)
        print(f"  [{tag}] 音符={state['notes']} 指纹={state['sig']} 钢琴缓冲={state['piano']}s")
        pg.evaluate("()=>stopMelody()")
        pg.wait_for_timeout(300)
        return state

    print("\n--- 1) 应用任务A 并播放 ---")
    apply(first)
    a = play_and_probe("A")

    print("\n--- 2) 切到任务B（模拟重新上传并扒谱完成）---")
    apply(second)
    after_switch = pg.evaluate("""() => ({
      piano: pianoData ? 'still-there' : 'cleared',
      sig: S.notesSig,
      notes: (S.playback&&S.playback.notes)?S.playback.notes.length:0,
    })""")
    print("  切换后:", after_switch)

    print("\n--- 3) 在任务B 上播放 ---")
    bb = play_and_probe("B")

    print("\n===== 结论 =====")
    print("  切换后旧缓冲已作废:", after_switch["piano"] == "cleared")
    print("  A 缓冲时长:", a["piano"], "s | B 缓冲时长:", bb["piano"], "s")
    print("  两者不同（=确实重新渲染了）:", a["piano"] != bb["piano"] or a["sig"] != bb["sig"])
    print("  指纹 A -> B:", a["sig"], "->", bb["sig"])
    ok = after_switch["piano"] == "cleared" and a["sig"] != bb["sig"]
    print("\n  判定:", "✓ 试听跟着换歌了" if ok else "✗ 仍是上一首")
    print("控制台/页面错误:", errs or "（无）")
    b.close()
