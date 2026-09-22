"""给钢琴卷帘拍图：空态 + 用合成音符铺满的效果。

用法：
  python _smoke\\shot_roll.py [URL] [OUT_PREFIX]

第二张图用**注入的合成数据**（不跑推理），只是为了看渲染/网格/歌词层的观感。
"""
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799/"
PREFIX = sys.argv[2] if len(sys.argv) > 2 else r"D:\AAA_Code_Project\_shot\roll"

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1280, "height": 1000}, device_scale_factor=2)
    pg.goto(URL, wait_until="domcontentloaded")
    pg.wait_for_function("() => typeof S !== 'undefined' && !!document.querySelector('#prHost')")
    pg.eval_on_selector("#scoreCard", "e => e.classList.remove('collapsed')")
    pg.wait_for_timeout(400)
    pg.locator("#scoreCard").screenshot(path=PREFIX + "_empty.png")
    print("saved", PREFIX + "_empty.png")

    # 注入一段看得过去的合成谱面：人声主旋律（带歌词与 - / + 记号）+ 器乐 + 和弦
    pg.evaluate("""
      () => {
        const mel = [60,62,64,65,67,65,64,62,60,60,62,64];
        const dur = [0.5,0.25,0.25,0.5,0.5,0.25,0.25,0.5,0.5,0.25,0.25,0.5];
        const lyr = ['我','-','爱','你','‿','+','+','-','la','la','la','la'];
        let t = 0; const notes = [];
        for (let i=0;i<mel.length;i++){ notes.push({start:t,end:t+dur[i],pitch:mel[i],lyric:lyr[i]}); t += dur[i]; }
        const ins = []; let u = 0;
        for (const q of [55,57,59,60,59,57]){ ins.push({start:u,end:u+0.5,pitch:q,lyric:'la'}); u += 0.5; }
        const chords = [];
        for (let k=0;k<6;k++){ const s=k*1.0;
          for (const q of [48,52,55]) chords.push({start:s,end:s+0.9,pitch:q,lyric:'la'}); }
        roll.load({
          bpm:120, meter:'4/4', duration:t,
          tempo_map:[{t:0,bpm:120}],
          tracks:[
            {voice:'Vocal', display:'主人声', is_vocal:true, notes:notes},
            {voice:'Ins', display:'器乐旋律', is_vocal:false, notes:ins}
          ],
          chord_notes: chords,
          has_vocal_lyrics:true
        });
      }
    """)
    pg.wait_for_timeout(500)
    pg.locator("#scoreCard").screenshot(path=PREFIX + "_notes.png")
    print("saved", PREFIX + "_notes.png")

    # 再拍一张：Ctrl+A 全选**当前曲目**（人声主旋律），准备整段铺歌词的样子。
    # 这张要能看出：曲目按钮、其他轨淡显、选中的一整条轨、歌词框进入批量态、
    # 以及标尺上的**播放头抓手**（定位用的那个小三角）。
    pg.evaluate("() => { S.playback = {notes: [], duration: 6}; roll.setPlayhead(1.6); }")
    pg.eval_on_selector(".pr", "e => e.focus()")
    pg.keyboard.press("Control+a")
    pg.wait_for_timeout(400)
    pg.locator("#scoreCard").screenshot(path=PREFIX + "_tracks.png")
    print("saved", PREFIX + "_tracks.png")
    b.close()
