"""验证：内置音色库可访问 + 浏览器里能用 ABCJS.synth 渲染出真钢琴 AudioBuffer。"""
import json

from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8777/"
errs = []
with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", args=["--autoplay-policy=no-user-gesture-required"])
    pg = b.new_page(viewport={"width": 1360, "height": 1000})
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)

    # 音色文件是否被静态挂载正确提供
    for note in ("C4.mp3", "A0.mp3", "C8.mp3"):
        r = pg.request.get(f"{B}vendor/soundfont/acoustic_grand_piano-mp3/{note}")
        print(f"  /vendor/soundfont/.../{note} -> {r.status} {len(r.body())} bytes")

    pg.goto(B, wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)
    print("ABCJS.synth 可用:", pg.evaluate("()=>!!(window.ABCJS && ABCJS.synth && ABCJS.synth.CreateSynth)"))

    # 注入一小段旋律并触发播放
    res = pg.evaluate("""async () => {
      const out = {};
      S.playback = {notes:[
        {start:0.0,end:0.5,pitch:60},{start:0.5,end:1.0,pitch:64},
        {start:1.0,end:1.5,pitch:67},{start:1.5,end:2.0,pitch:72}], duration:2.0};
      document.getElementById('tpPlay').disabled = false;
      document.getElementById('tpPlay').click();
      for (let i=0;i<120 && !pianoData;i++) await new Promise(r=>setTimeout(r,250));
      out.gotPiano = !!pianoData;
      if (pianoData) {
        out.bufferSeconds = Math.round(pianoData.buffer.duration*100)/100;
        out.channels = pianoData.buffer.numberOfChannels;
        out.gain = Math.round(pianoData.gain*100)/100;
      }
      out.playing = melodyPlaying;
      out.srcIsBuffer = !!tpSource && (tpSource.buffer === (pianoData&&pianoData.buffer));
      out.hint = document.getElementById('toneHint').textContent;
      return out;
    }""")
    print("\n渲染结果:")
    for k, v in res.items():
        print(f"  {k} = {v}")

    pg.wait_for_timeout(1200)
    print("\n播放中进度条宽度:", pg.eval_on_selector("#tpFill", "e=>e.style.width"))
    print("时间显示:", pg.text_content("#tpTime"))
    pg.evaluate("()=>stopMelody()")
    print("\n控制台/页面错误:", errs or "（无）")
    b.close()
