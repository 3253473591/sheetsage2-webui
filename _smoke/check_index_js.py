"""从 web/index.html 抽出 <script>...</script> 块写到 _index_inline.js（供 node --check 校验）。"""
import pathlib
import re

root = pathlib.Path(__file__).resolve().parent.parent
html = (root / "web" / "index.html").read_text(encoding="utf-8")
blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
out = pathlib.Path(__file__).resolve().parent / "_index_inline.js"
out.write_text("\n;\n".join(blocks), encoding="utf-8")
print("inline script blocks:", len(blocks), "->", out)
