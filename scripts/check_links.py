"""链接体检：README 里写得出的每一个链接，都得点得到。

为什么要有这个脚本：
    上一轮把传播稿 `docs/` 移出了仓库（不随 Git 公开），但 README 里还
    留着 4 个指向它的 Markdown 链接。**本地全都点得开，远端全部 404** ——
    因为那些文件根本没被推上去。这类死链靠眼睛看不出来：本地预览一切正常，
    只有把仓库 clone 到一台新机器上才会暴露，而那时看到的人已经是路人了。

    所以它同时检查三件事，缺一件都不算「点得到」：
      1. 页内锚点 `#xxx` 有没有对应的 `<a id="xxx">`；
      2. 相对路径指向的文件是否**存在且被 Git 跟踪**（存在但不跟踪 = 远端死链）；
      3. 外链是否返回 200（默认不联网，加 `--online` 才查，避免 CI 被网络抖动搞红）。

用法：
    python scripts/check_links.py                 # 离线：锚点 + 相对路径
    python scripts/check_links.py --online        # 再联网查外链
    python scripts/check_links.py --all           # 扫仓库里所有被跟踪的 .md

    # 本机 env 里的 https_proxy 要是不可用的（很常见：代理软件换了端口），
    # urllib 会照着它连，然后报「网络不通」——看起来像死链，其实链接是好的。
    # 这时显式指定一个能用的代理：
    python scripts/check_links.py --online --proxy http://127.0.0.1:7897

退出码：
    0 = 全部点得到；1 = 有死链（会列出具体位置）
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 链接的两种写法：`[文字](目标)` 和裸写的 <https://...>
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_ANCHOR_DEF = re.compile(r'<a id="([^"]+)"></a>')

# 代码块里的反引号、命令行示例不该被当成链接（比如 `pip install xxx` 不是链接）
_FENCE = re.compile(r"```.*?```", re.S)


def _tracked_files() -> set[str]:
    """Git 真正会推上去的文件 —— 存在但不在里面的，远端就是 404。"""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if out.returncode != 0 or not out.stdout.strip():
        # 不在 Git 仓库里（比如被打包成 zip 下载）时，退化成「存在就算通过」
        return set()
    return set(out.stdout.splitlines())


def _scan(md: pathlib.Path, tracked: set[str], online: bool, proxy: str = "") -> list[str]:
    text = md.read_text(encoding="utf-8")
    body = _FENCE.sub("", text)  # 去掉代码块：示例命令不是链接
    rel_dir = md.parent

    bad: list[str] = []
    anchors = set(_ANCHOR_DEF.findall(text))

    for target in _LINK.findall(body):
        if target.startswith("#"):
            if target[1:] not in anchors:
                bad.append(f"{md.name}: 页内跳转 {target} 找不到锚点")
        elif target.startswith(("http://", "https://")):
            if not online:
                continue
            bad.extend(_check_http(md.name, target, proxy))
        elif target.startswith("mailto:"):
            continue
        else:
            bad.extend(_check_local(md.name, rel_dir / target, target, tracked))
    return bad


def _check_local(name: str, path: pathlib.Path, raw: str, tracked: set[str]) -> list[str]:
    if not path.exists():
        return [f"{name}: {raw} —— 文件不存在"]

    if not tracked:
        return []  # 非 Git 环境，无法判断跟踪状态

    rel = path.resolve().relative_to(ROOT).as_posix()
    if rel not in tracked:
        return [
            f"{name}: {raw} —— 文件在本地有，但没被 Git 跟踪，"
            f"推上去就是 404（要么 git add，要么别放链接）"
        ]
    return []


def _safe_url(url: str) -> str:
    """把 URL 里的非 ASCII 字符转成 percent 编码 —— 浏览器会自动做，urllib 不会。

    徽章里写了中文（`badge/依赖-仅标准库`），不编码的话 urllib 直接抛
    UnicodeEncodeError，看起来像"链接挂了"，其实链接是好的。
    """
    if url.isascii():
        return url
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            urllib.parse.quote(parts.path, safe="/%"),
            urllib.parse.quote(parts.query, safe="/%=&"),
            parts.fragment,
        )
    )


def _check_http(name: str, url: str, proxy: str = "") -> list[str]:
    req = urllib.request.Request(
        _safe_url(url), method="GET", headers={"User-Agent": "mcd-butler-link-check"}
    )
    try:
        if proxy:
            # 显式指定代理：绕开 env 里那个可能已经失效的 https_proxy
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            resp_ctx = opener.open(req, timeout=20)
        else:
            resp_ctx = urllib.request.urlopen(req, timeout=20)
        with resp_ctx as resp:
            if resp.status >= 400:
                return [f"{name}: {url} —— HTTP {resp.status}"]
    except urllib.error.HTTPError as exc:
        return [f"{name}: {url} —— HTTP {exc.code}"]
    except Exception as exc:  # 超时 / DNS / SSL：只提示，不算死链
        return [f"{name}: {url} —— 网络不通（{type(exc).__name__}），请人工确认"]
    return []


def main(argv: list[str]) -> int:
    online = "--online" in argv
    scan_all = "--all" in argv
    proxy = ""
    for i, arg in enumerate(argv):
        if arg == "--proxy" and i + 1 < len(argv):
            proxy = argv[i + 1]
        elif arg.startswith("--proxy="):
            proxy = arg.split("=", 1)[1]

    tracked = _tracked_files()
    if scan_all and tracked:
        targets = [ROOT / p for p in sorted(tracked) if p.endswith(".md")]
    else:
        targets = [ROOT / "README.md"]

    dead: list[str] = []
    for md in targets:
        if md.exists():
            dead.extend(_scan(md, tracked, online, proxy))

    print(
        f"检查 {len(targets)} 个文件"
        f"（外链{'联网' if online else '跳过'}"
        f"{'，走代理 ' + proxy if proxy else ''}）"
    )
    if dead:
        print(f"\n发现 {len(dead)} 处点不到的链接：")
        for item in dead:
            print(f"  ✗ {item}")
        return 1
    print("全部链接都点得到 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
