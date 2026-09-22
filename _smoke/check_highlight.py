"""可行性验证：abcjs 渲染出的音符元素数量 / 顺序，能否与后端音符列表一一对应。

如果 .abcjs-note 的数量和顺序与 /api/tasks/{id}/notes 返回的音符一致，
高亮就只是「按下标套 class」，难度很低；否则需要用字符位置映射，难度上一个台阶。
"""
from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8777/"
ABC = """X:1
T:
M:4/4
L:1/16
Q:1/4=75
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
K:Eb
% verse
V: Vocal
"Cm"e3dd2ee-"G#"e2z4e2|"A#"e2d2c2dc-"D#"cB3z2B2|"Cm"B4A2GB-"G#"B2z4B2|
"""

with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1200, "height": 900})
    pg.goto(B, wait_until="domcontentloaded")
    pg.wait_for_timeout(2000)

    info = pg.evaluate("""(abc) => {
      const out = {};
      const tmp = document.createElement('div');
      ABCJS.renderAbc(tmp, abc, {scale:1.0, staffwidth:640, add_classes:true, showMeter:true});
      document.body.appendChild(tmp);
      out.totalSvg = tmp.querySelectorAll('svg').length;
      out.noteEls = tmp.querySelectorAll('.abcjs-note').length;
      out.restEls = tmp.querySelectorAll('.abcjs-rest').length;
      out.noteheadEls = tmp.querySelectorAll('.abcjs-notehead').length;
      out.stemEls = tmp.querySelectorAll('.abcjs-stem').length;
      // 每个 .abcjs-note 里是否有 notehead
      const notes = [...tmp.querySelectorAll('.abcjs-note')];
      out.firstNoteHtml = notes.length ? notes[0].outerHTML.slice(0,180) : null;
      // 试着给第 3 个音符加高亮，看是否可见
      if (notes.length > 2) {
        notes[2].classList.add('hl-test');
        const s = document.createElement('style');
        s.textContent = '.hl-test .abcjs-notehead, .hl-test path { fill:#e0409b !important; stroke:#e0409b !important; }';
        document.head.appendChild(s);
        out.hlApplied = true;
        out.hlRect = JSON.stringify(notes[2].getBoundingClientRect());
      }
      tmp.remove();
      return out;
    }""", ABC)

    for k, v in info.items():
        print(f"  {k} = {v}")

    # 与后端 /notes 的数量对照（用现有 smoke_v4 的乐谱文件内容）
    pg.evaluate("""() => { S.rawAbc = null; }""")
    n = pg.evaluate("""async (abc) => {
      const tmp = document.createElement('div');
      ABCJS.renderAbc(tmp, abc, {scale:1.0, staffwidth:640, add_classes:true});
      const count = tmp.querySelectorAll('.abcjs-note').length;
      tmp.remove();
      return count;
    }""", ABC)
    print(f"\nabcjs 音符元素数 = {n}")
    b.close()
