"""Windows 打包脚本:python build.py → dist/AI短视频生成器_win64.zip

自动完成:
1. 安装 PyInstaller(如缺失);
2. 下载 ffmpeg 精简版并抽取 ffmpeg.exe / ffprobe.exe(如缺失);
3. 打包为免安装的绿色目录(内置 ffmpeg,用户无需安装 Python 和 ffmpeg);
4. 连同 config.yaml、music/、README 一起压缩为发布 zip。

普通用户无需运行本脚本——直接从 GitHub Releases 下载打包好的 zip 即可。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "AI短视频生成器"
FFMPEG_DIR = ROOT / "ffmpeg"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

# Windows 控制台/CI 默认编码可能是 cp1252/GBK,打印中文会崩,强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def ensure_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        print(">> 安装 PyInstaller …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def ensure_ffmpeg() -> bool:
    """确保 ffmpeg/ffmpeg.exe 与 ffprobe.exe 就位,返回是否可捆绑。"""
    if sys.platform != "win32":
        print(">> 非 Windows 平台,跳过捆绑 ffmpeg(打包产物需系统自带 ffmpeg)")
        return False
    exes = [FFMPEG_DIR / "ffmpeg.exe", FFMPEG_DIR / "ffprobe.exe"]
    if all(p.exists() for p in exes):
        return True

    FFMPEG_DIR.mkdir(exist_ok=True)
    archive = FFMPEG_DIR / "_ffmpeg_download.zip"
    print(f">> 下载 ffmpeg: {FFMPEG_URL}")
    urllib.request.urlretrieve(FFMPEG_URL, archive)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            name = Path(member).name
            if name in ("ffmpeg.exe", "ffprobe.exe"):
                with zf.open(member) as src, open(FFMPEG_DIR / name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    archive.unlink(missing_ok=True)
    missing = [p.name for p in exes if not p.exists()]
    if missing:
        raise RuntimeError(f"ffmpeg 压缩包中未找到: {missing}")
    return True


def main() -> None:
    ensure_pyinstaller()
    bundle_ffmpeg = ensure_ffmpeg()
    sep = ";" if sys.platform == "win32" else ":"

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", APP_NAME,
        # config.yaml 模板打进包里,首次运行自动生成到 exe 旁
        "--add-data", f"config.yaml{sep}.",
    ]
    if bundle_ffmpeg:
        args += [
            "--add-binary", f"{FFMPEG_DIR / 'ffmpeg.exe'}{sep}ffmpeg",
            "--add-binary", f"{FFMPEG_DIR / 'ffprobe.exe'}{sep}ffmpeg",
        ]
    args.append(str(ROOT / "main.py"))

    print(">> PyInstaller 打包 …")
    subprocess.check_call(args, cwd=ROOT)

    dist = ROOT / "dist" / APP_NAME
    print(">> 组装发布目录 …")
    shutil.copyfile(ROOT / "config.yaml", dist / "config.yaml")
    (dist / "music").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "music" / "说明.txt", dist / "music" / "说明.txt")
    shutil.copyfile(ROOT / "README.md", dist / "README.md")

    print(">> 压缩 …")
    archive = shutil.make_archive(
        str(ROOT / "dist" / f"{APP_NAME}_win64"), "zip",
        root_dir=ROOT / "dist", base_dir=APP_NAME,
    )
    print(f"完成: {archive}")


if __name__ == "__main__":
    main()
