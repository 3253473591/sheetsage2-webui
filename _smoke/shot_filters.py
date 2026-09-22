"""展开 ③ 乐谱编辑，拍「导出内容」三个勾选那一行。"""
import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799/"
OUT = sys.argv[2] if len(sys.argv) > 2 else r"D:\AAA_Code_Project\_shot\ui_filters.png"

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1180, "height": 900}, device_scale_factor=2)
    pg.goto(URL, wait_until="domcontentloaded")
    pg.wait_for_function("() => typeof S !== 'undefined' && !!document.querySelector('#btnStart')")
    pg.eval_on_selector('button.fold[data-fold="scoreCard"]', "e => e.click()")
    pg.wait_for_timeout(300)
    pg.locator("section#scoreCard .filters").screenshot(path=OUT)
    print("saved", OUT)
    b.close()
