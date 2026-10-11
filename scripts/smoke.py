"""本轮整改后的完整冒烟：编译 / 3.10 语法 / 自检 / 演示 / 全命令参数 / 编码。"""
import ast
import pathlib
import subprocess
import sys

PY = sys.executable
ROOT = pathlib.Path(__file__).resolve().parent.parent
ok = True


def run(args, env=None, label=""):
    r = subprocess.run(
        [PY, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env
    )
    flag = "OK " if r.returncode == 0 else "FAIL"
    if r.returncode != 0:
        globals()["ok"] = False
    tail = (r.stdout.strip().splitlines() or [""])[-1][:60]
    print(f"  [{flag}] {label or ' '.join(args)}  → {tail}")
    return r


print("== 1. 编译 ==")
run(["-m", "compileall", "-q", "src"], label="compileall src")

print("== 2. Python 3.10 语法兼容 ==")
bad = []
for f in sorted(ROOT.rglob("*.py")):
    if ".workbuddy" in str(f):
        continue
    try:
        ast.parse(f.read_text(encoding="utf-8"), feature_version=(3, 10))
    except SyntaxError as e:
        bad.append(f"{f}: {e}")
print(f"  {'OK ' if not bad else 'FAIL'} {len(list(ROOT.rglob('*.py')))} 个文件，3.10 不兼容 {len(bad)} 个")
if bad:
    ok = False
    for b in bad:
        print("   ", b)

print("== 3. 离线自检 ==")
r = run(["-m", "src.cli", "selftest"], label="selftest")
print("     ", [l for l in r.stdout.splitlines() if "通过" in l][-1].strip())

print("== 4. 离线演示 ==")
run(["-m", "src.cli", "demo"], label="demo")

print("== 5. 全部子命令 --help ==")
import re

help_txt = subprocess.run(
    [PY, "-m", "src.cli", "--help"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
).stdout
cmds = re.findall(r"\{([a-z,\-]+)\}", help_txt)
cmds = cmds[0].split(",") if cmds else []
for c in cmds:
    run(["-m", "src.cli", c, "--help"], label=f"{c} --help")

print("== 6. 各代码页下不崩 ==")
import os

for enc in ("utf-8", "gbk", "cp936", "cp1252"):
    env = dict(os.environ, PYTHONIOENCODING=enc)
    for args in (["-m", "src.cli", "selftest"], ["-m", "src.cli", "demo"]):
        run(args, env=env, label=f"{enc} {' '.join(args[2:])}")

print("\n结论:", "全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)
