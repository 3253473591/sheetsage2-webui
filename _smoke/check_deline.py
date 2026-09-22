"""验证 abcjs 的内建按时间高亮（deline / TimingCallbacks），决定高亮实现难度。"""
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

    r = pg.evaluate("""(abc) => {
      const out = {};
      const div = document.createElement('div');
      document.body.appendChild(div);
      const tunes = ABCJS.renderAbc(div, abc, {scale:1.0, staffwidth:640, add_classes:true});
      const t = tunes[0];
      out.hasDeline = typeof t.deline;
      out.hasTotalTime = typeof t.getTotalTime === 'function' ? t.getTotalTime() : null;
      out.hasSetTiming = typeof t.setTiming;
      out.hasSetupEvents = typeof t.setupEvents;
      out.hasGetElFromChar = typeof t.getElementFromChar;
      out.millisPerMeasure = typeof t.millisecondsPerMeasure;
      out.totalBeats = typeof t.getTotalBeats === 'function' ? t.getTotalBeats() : null;
      out.barLength = typeof t.getBarLength === 'function' ? t.getBarLength() : null;

      // 试试 deline（abcjs 的光标/高亮 API）
      const before = div.querySelectorAll('[class*="abcjs-highlight"], .abcjs-cursor').length;
      let delErr = null, delOk = false;
      try {
        if (typeof t.deline === 'function') {
          t.deline({milliseconds: 1500, klass: 'abcjs-highlight'});
          delOk = true;
        }
      } catch(e) { delErr = String(e); }
      out.delineOk = delOk; out.delineErr = delErr;
      out.highlightAfter = div.querySelectorAll('[class*="abcjs-highlight"], .abcjs-cursor').length;
      // 哪个元素被加类了
      const hl = div.querySelector('.abcjs-highlight, [class*="abcjs-highlight"]');
      out.hlTag = hl ? hl.tagName + '.' + hl.getAttribute('class') : null;

      // TimingCallbacks 是否可用（可用来驱动高亮）
      out.hasTimingCallbacks = typeof (ABCJS.TimingCallbacks);
      out.hasCursorControl = typeof (ABCJS.CursorControl);
      return out;
    }""", ABC)
    for k, v in r.items():
        print(f"  {k} = {v}")
    b.close()
