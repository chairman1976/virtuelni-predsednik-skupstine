# PyInstaller recipe for the Windows .exe build - run build_exe.bat (clean venv with requirements.txt + pyinstaller).
# Output: dist/VirtuelniPredsednik/  (VirtuelniPredsednik.exe + _internal/; build_exe.bat adds data/ and bin/)
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs, collect_submodules

datas, binaries, hidden = [], [], []
for pkg in ("faster_whisper", "ctranslate2", "sherpa_onnx", "winrt", "sounddevice", "_sounddevice_data"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h
datas += collect_data_files("certifi")
hidden += collect_submodules("winrt") + ["winocr", "anthropic", "onnxruntime", "av", "tokenizers"]
# GPU speech recognition: cuBLAS + cuDNN from the nvidia-* wheels -> _internal/nvidia/<lib>/bin (common.setup_env)
for lib in ("cublas", "cudnn", "cuda_nvrtc"):
    binaries += [(src, dest) for src, dest in collect_dynamic_libs("nvidia." + lib)]

a = Analysis(
    ["app.py"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["torch", "tensorflow", "matplotlib", "scipy", "pandas", "IPython", "tkinter", "PyQt5", "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtQuick", "PyQt6.QtQml", "PyQt6.Qt3DCore",
              "PyQt6.QtMultimedia", "PyQt6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VirtuelniPredsednik", console=False,
          upx=False, icon=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VirtuelniPredsednik")
