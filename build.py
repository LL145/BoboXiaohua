"""跨平台打包脚本:python build.py → dist/AI-Video-Generator_<平台>.zip / .tar.gz

支持 Windows / Linux / macOS(均为 64 位),自动完成:
1. 安装 PyInstaller(如缺失);
2. 下载对应平台的 ffmpeg 静态版并抽取 ffmpeg / ffprobe(多源重试,如缺失);
3. 打包为免安装的绿色目录(内置 ffmpeg,用户无需安装 Python 和 ffmpeg);
4. 连同 config.yaml、music/、README 一起压缩为发布包
   (Windows 为 zip,Linux/macOS 为 tar.gz 以保留可执行权限)。

普通用户无需运行本脚本——直接从 GitHub Releases 下载打包好的压缩包即可。
"""

from __future__ import annotations

import importlib.util
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "AI短视频生成器"
ARCHIVE_BASE = "AI-Video-Generator"  # GitHub Release 附件不支持中文文件名
FFMPEG_DIR = ROOT / "ffmpeg"

# 各平台 ffmpeg 静态版下载源:外层按顺序尝试(主源失败自动换备用源),
# 内层是该源需要下载的压缩包(Windows/Linux 一个包内含两件工具,macOS 分两个包)。
_MACOS_ARCH = "arm64" if platform.machine() == "arm64" else "amd64"
FFMPEG_MIRRORS: dict[str, list[list[str]]] = {
    "win32": [
        ["https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"],
        ["https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"],
    ],
    "linux": [
        ["https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"],
        ["https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-linux64-gpl.tar.xz"],
    ],
    "darwin": [
        [
            f"https://ffmpeg.martin-riedl.de/redirect/latest/macos/{_MACOS_ARCH}/release/ffmpeg.zip",
            f"https://ffmpeg.martin-riedl.de/redirect/latest/macos/{_MACOS_ARCH}/release/ffprobe.zip",
        ],
        [
            # 备用源仅 x86_64,Apple 芯片上经 Rosetta 运行
            "https://evermeet.cx/ffmpeg/getrelease/zip",
            "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip",
        ],
    ],
}

# Windows 控制台/CI 默认编码可能是 cp1252/GBK,打印中文会崩,强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def ensure_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        print(">> 安装 PyInstaller …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def ensure_requirements() -> None:
    """确保运行依赖齐全(尤其 edge-tts),漏装会导致打包版缺少旁白配音功能。"""
    if any(
        importlib.util.find_spec(mod) is None
        for mod in ("fal_client", "yaml", "requests", "edge_tts")
    ):
        print(">> 安装运行依赖 …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
        )


def _download(url: str, dest: Path, attempts: int = 3) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(1, attempts + 1):
        try:
            print(f">> 下载 {url}" + (f"(第 {attempt} 次尝试)" if attempt > 1 else ""))
            with urllib.request.urlopen(request, timeout=120) as resp, open(dest, "wb") as f:
                shutil.copyfileobj(resp, f)
            return
        except Exception as exc:  # noqa: BLE001 网络错误种类繁多,统一重试
            if attempt == attempts:
                raise
            wait = 5 * attempt
            print(f"   下载失败({exc}),{wait} 秒后重试")
            time.sleep(wait)


def _extract_tools(archive: Path, wanted: set[str]) -> None:
    """从 zip / tar.* 压缩包中按文件名抽取 ffmpeg、ffprobe 到 FFMPEG_DIR。"""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if not member.endswith("/") and Path(member).name in wanted:
                    with zf.open(member) as src, open(FFMPEG_DIR / Path(member).name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if member.isfile() and Path(member.name).name in wanted:
                    with tf.extractfile(member) as src, open(FFMPEG_DIR / Path(member.name).name, "wb") as dst:
                        shutil.copyfileobj(src, dst)


def ensure_ffmpeg() -> bool:
    """确保 ffmpeg/ 下的 ffmpeg 与 ffprobe 就位,返回是否可捆绑。"""
    suffix = ".exe" if sys.platform == "win32" else ""
    tools = [FFMPEG_DIR / f"ffmpeg{suffix}", FFMPEG_DIR / f"ffprobe{suffix}"]
    if all(p.exists() for p in tools):
        return True

    mirrors = FFMPEG_MIRRORS.get(sys.platform)
    if mirrors is None:
        print(f">> 未知平台 {sys.platform},跳过捆绑 ffmpeg(打包产物需系统自带 ffmpeg)")
        return False

    FFMPEG_DIR.mkdir(exist_ok=True)
    wanted = {p.name for p in tools}
    archive = FFMPEG_DIR / "_ffmpeg_download.tmp"
    last_error: Exception | None = None
    for mirror in mirrors:
        try:
            for url in mirror:
                _download(url, archive)
                _extract_tools(archive, wanted)
                archive.unlink(missing_ok=True)
            missing = [p.name for p in tools if not p.exists()]
            if missing:
                raise RuntimeError(f"压缩包中未找到: {missing}")
            if not suffix:
                for p in tools:
                    p.chmod(0o755)
            return True
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            archive.unlink(missing_ok=True)
            print(f"   该下载源不可用({exc}),尝试备用源 …")
    raise RuntimeError(f"所有 ffmpeg 下载源均失败,最后错误: {last_error}")


def _platform_tag() -> str:
    if sys.platform == "win32":
        return "win64"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine() == "arm64" else "macos-intel"
    return "linux64"


def main() -> None:
    ensure_requirements()
    ensure_pyinstaller()
    bundle_ffmpeg = ensure_ffmpeg()
    sep = ";" if sys.platform == "win32" else ":"
    suffix = ".exe" if sys.platform == "win32" else ""

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", APP_NAME,
        # config.yaml 模板打进包里,首次运行自动生成到程序旁
        "--add-data", f"config.yaml{sep}.",
    ]
    if bundle_ffmpeg:
        args += [
            "--add-binary", f"{FFMPEG_DIR / ('ffmpeg' + suffix)}{sep}ffmpeg",
            "--add-binary", f"{FFMPEG_DIR / ('ffprobe' + suffix)}{sep}ffmpeg",
        ]
    args.append(str(ROOT / "main.py"))

    print(">> PyInstaller 打包 …")
    subprocess.check_call(args, cwd=ROOT)

    dist = ROOT / "dist"
    if sys.platform == "darwin":
        # macOS 发布 .app 包:配置与输出生成在 .app 旁边(见 config.app_dir)
        stage = dist / "_pkg" / APP_NAME
        if stage.parent.exists():
            shutil.rmtree(stage.parent)
        stage.mkdir(parents=True)
        shutil.move(str(dist / f"{APP_NAME}.app"), str(stage / f"{APP_NAME}.app"))
        pkg_root, pkg_dir = stage.parent, stage
    else:
        pkg_root, pkg_dir = dist, dist / APP_NAME

    print(">> 组装发布目录 …")
    shutil.copyfile(ROOT / "config.yaml", pkg_dir / "config.yaml")
    (pkg_dir / "music").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "music" / "说明.txt", pkg_dir / "music" / "说明.txt")
    shutil.copyfile(ROOT / "README.md", pkg_dir / "README.md")

    print(">> 压缩 …")
    fmt = "zip" if sys.platform == "win32" else "gztar"  # tar.gz 保留可执行权限
    archive = shutil.make_archive(
        str(dist / f"{ARCHIVE_BASE}_{_platform_tag()}"), fmt,
        root_dir=pkg_root, base_dir=APP_NAME,
    )
    print(f"完成: {archive}")


if __name__ == "__main__":
    main()
