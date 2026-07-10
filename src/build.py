"""PyInstaller 打包脚本 — 将 src/ 打包为 exe，nginx 运行时外置"""
import os
import shutil
import subprocess
import sys

STREAMING = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(STREAMING, "src")
DIST = os.path.join(STREAMING, "dist")
NGINX_SRC = os.path.join(STREAMING, "rtmp_server", "nginx")

# 1) 清空 dist
if os.path.exists(DIST):
    shutil.rmtree(DIST)
os.makedirs(DIST)

# 2) PyInstaller 打包
spec = f"""# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    [r'{SRC}\\main.py'],
    pathex=[r'{SRC}'],
    binaries=[],
    datas=[],
    hiddenimports=['dnslib', 'dnslib.dns', 'dnslib.label', 'dnslib.bitmap'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PS5直播截获工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
"""

spec_path = os.path.join(STREAMING, "build.spec")
with open(spec_path, "w", encoding="utf-8") as f:
    f.write(spec)

subprocess.run(
    [sys.executable, "-m", "PyInstaller", "--distpath", DIST, "--workpath",
     os.path.join(STREAMING, "build_temp"), "--specpath", STREAMING, spec_path],
    cwd=STREAMING, check=True,
)

# 3) 复制 nginx 运行时到 dist
nginx_dst = os.path.join(DIST, "rtmp_server", "nginx")
shutil.copytree(NGINX_SRC, nginx_dst)

# 4) 清理 spec
os.remove(spec_path)
build_temp = os.path.join(STREAMING, "build_temp")
if os.path.exists(build_temp):
    shutil.rmtree(build_temp)

print(f"\n打包完成: {os.path.join(DIST, 'PS5直播截获工具.exe')}")
