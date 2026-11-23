"""Build the Windows release: dist/VirtuelniPredsednik/ (+ .zip) with the .exe, data/ and bin/ tools.

Run with a clean venv that has requirements.txt + pyinstaller installed:
    python build_exe.py <work_dir>
Needs ffmpeg.exe and node.exe on PATH (they are copied into bin/); yt-dlp.exe is downloaded.
"""
import glob
import os
import shutil
import ssl
import subprocess
import sys
import urllib.request

import certifi

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "vp_build"))
DIST = os.path.join(WORK, "dist", "VirtuelniPredsednik")
YTDLP = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"


def fix_qt_runtime():
    """Qt may ship an older msvcp140.dll than onnxruntime/CTranslate2 need - use the system's (newest) one."""
    import PyQt6
    qbin = os.path.join(os.path.dirname(PyQt6.__file__), "Qt6", "bin")
    for f in glob.glob(os.path.join(qbin, "*.dll")):
        name = os.path.basename(f).lower()
        sysf = os.path.join(os.environ["WINDIR"], "System32", name)
        if name.startswith(("msvcp140", "vcruntime140", "concrt140")) and os.path.exists(sysf):
            shutil.copyfile(sysf, f)


def main():
    fix_qt_runtime()
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                           "--distpath", os.path.join(WORK, "dist"), "--workpath", os.path.join(WORK, "work"),
                           os.path.join(HERE, "VirtuelniPredsednik.spec")], cwd=HERE)
    # data next to the .exe (writable: profiles cache); never settings_local.json / sessions
    shutil.copytree(os.path.join(HERE, "data"), os.path.join(DIST, "data"), dirs_exist_ok=True)
    os.makedirs(os.path.join(DIST, "bin"), exist_ok=True)
    for tool in ("ffmpeg", "node"):
        src = shutil.which(tool)
        if src and src.lower().endswith(".exe") is False:
            src = shutil.which(tool + ".exe")
        assert src, tool + " not found on PATH"
        shutil.copyfile(os.path.realpath(src), os.path.join(DIST, "bin", tool + ".exe"))
    ca = os.environ.get("PRED_CA_BUNDLE") or os.path.expanduser(r"~\.certs\ca-bundle-with-avg.pem")
    ctx = ssl.create_default_context(cafile=ca if os.path.exists(ca) else certifi.where())
    with urllib.request.urlopen(YTDLP, context=ctx, timeout=120) as r, \
            open(os.path.join(DIST, "bin", "yt-dlp.exe"), "wb") as f:
        shutil.copyfileobj(r, f)
    shutil.copyfile(os.path.join(HERE, "README.md"), os.path.join(DIST, "README.md"))
    with open(os.path.join(DIST, "bin", "LICENSES.txt"), "w", encoding="utf-8") as f:
        f.write("ffmpeg.exe - FFmpeg (GPL), build by gyan.dev, source: https://ffmpeg.org / https://www.gyan.dev/ffmpeg/builds/\n"
                "node.exe - Node.js (MIT), https://nodejs.org\n"
                "yt-dlp.exe - yt-dlp (Unlicense), https://github.com/yt-dlp/yt-dlp\n")
    print("built", DIST)


if __name__ == "__main__":
    main()
