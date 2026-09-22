"""给 ①②③ 拍一张首屏截图，肉眼确认三个勾选默认状态与间距。"""
import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799/"
OUT = sys.argv[2] if len(sys.argv) > 2 else r"D:\AAA_Code_Project\_shot\ui_voices.png"

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1180, "height": 1000}, device_scale_factor=2)
    pg.goto(URL, wait_until="domcontentloaded")
    pg.wait_for_function("() => typeof S !== 'undefined' && !!document.querySelector('#btnStart')")
    pg.wait_for_timeout(400)
    # 只截 ①②③ 三张卡片，导出卡片没变化
    pg.locator("section.card").nth(0).screenshot(path=OUT.replace(".png", "_card1.png"))
    # 首屏整图
    pg.screenshot(path=OUT)
    print("saved", OUT)
    b.close()
