"""把真实的 `demo` 输出渲染成终端截图 SVG。

为什么要有这个脚本
------------------
README 的首图如果是一张手写的"仿终端图"，懂行的人一眼看出是假的，反而掉分。
所以这里不做图，只做**排版**：内容是 `python -m src.cli demo` 的真实 stdout，
脚本只负责把它摆进一个终端窗口里。改了输出，重跑一次脚本就同步。

用法：
    python scripts/render_terminal_svg.py [输出路径]

默认写到 screenshots/demo.svg。
"""

from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path

FONT = "'Cascadia Code','Consolas','SF Mono','Microsoft YaHei Mono',monospace"
BG = "#1e1e2e"
BAR = "#181825"
FG = "#cdd6f4"

# 行首特征 → 颜色
PALETTE = [
    ("宜 ·", "#a6e3a1"),  # 绿：宜
    ("忌 ·", "#f38ba8"),  # 红：忌
    ("══", "#f9e2af"),  # 黄：黄历标题
    ("麦门黄历（", "#89b4fa"),
    ("麦门段位（", "#89b4fa"),
    ("  段位", "#f9e2af"),
    ("  累计省下", "#a6e3a1"),
    ("  今天", "#f9e2af"),
    ("  ⚠️", "#f9e2af"),
    ("  （", "#6c7086"),
]


def color_of(line: str) -> str:
    for prefix, color in PALETTE:
        if line.lstrip().startswith(prefix.lstrip()) and prefix.strip() in line:
            return color
    if "段位" in line and "起" in line:
        return "#89b4fa"
    return FG


def pick(lines: list[str]) -> list[str]:
    """取 demo 输出里最有代表性的两段：黄历 + 段位。"""
    start = next((i for i, s in enumerate(lines) if "麦门黄历（almanac）" in s), None)
    if start is None:
        return lines[-40:]
    return [s for s in lines[start:] if s.strip()]


def render(lines: list[str], width: int = 820) -> str:
    pad_x, top, line_h = 28, 74, 22
    body = []
    for i, line in enumerate(lines):
        y = top + i * line_h
        body.append(
            f'  <text x="{pad_x}" y="{y}" fill="{color_of(line)}" '
            f'font-size="14" xml:space="preserve">{html.escape(line)}</text>'
        )
    height = top + len(lines) * line_h + 26
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'font-family="{FONT}">',
            f'  <rect width="{width}" height="{height}" rx="14" fill="{BG}"/>',
            f'  <rect width="{width}" height="40" rx="14" fill="{BAR}"/>',
            f'  <rect y="26" width="{width}" height="14" fill="{BAR}"/>',
            '  <circle cx="24" cy="20" r="6" fill="#ff5f56"/>',
            '  <circle cx="44" cy="20" r="6" fill="#ffbd2e"/>',
            '  <circle cx="64" cy="20" r="6" fill="#27c93f"/>',
            f'  <text x="{width // 2}" y="25" fill="#a6accd" font-size="13" '
            f'text-anchor="middle">mcd-butler · 真实 demo 输出渲染（非手绘图）</text>',
            "",
            *body,
            "</svg>",
            "",
        ]
    )


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "screenshots" / "demo.svg"

    proc = subprocess.run(
        [sys.executable, "-m", "src.cli", "demo"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return 1

    lines = pick(proc.stdout.splitlines())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(lines), encoding="utf-8")
    print(f"已写入 {out_path}（{len(lines)} 行，来自真实 demo 输出）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
