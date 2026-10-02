"""生成虚构的 SVG 照片（本地文件，不依赖任何外部服务或图片库）。"""
import os
from xml.sax.saxutils import escape


def write_photo(directory, name, title, subtitle="", accent=False):
    """写一张虚构照片：灰白为主，仅结论性内容可用绿色点缀。"""
    color = "#1a7f37" if accent else "#2b2f33"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400">
  <rect width="640" height="400" fill="#f3f4f6"/>
  <rect x="24" y="24" width="592" height="352" fill="#ffffff" stroke="#d1d5db" stroke-width="2"/>
  <rect x="56" y="64" width="230" height="170" fill="#e5e7eb" stroke="#9ca3af" stroke-width="2"/>
  <circle cx="171" cy="149" r="56" fill="#d1d5db" stroke="#6b7280" stroke-width="3"/>
  <circle cx="171" cy="149" r="28" fill="#9ca3af"/>
  <rect x="56" y="250" width="230" height="14" fill="#e5e7eb"/>
  <rect x="56" y="274" width="160" height="14" fill="#e5e7eb"/>
  <text x="320" y="120" font-family="monospace" font-size="24" font-weight="bold" fill="{color}">{escape(title)}</text>
  <text x="320" y="158" font-family="monospace" font-size="15" fill="#6b7280">{escape(subtitle)}</text>
  <text x="320" y="330" font-family="monospace" font-size="12" fill="#9ca3af">虚构照片 · 仅用于演示</text>
</svg>
"""
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
        fh.write(svg)
    return name
