"""Build with an isolated DLL search path, then run the packaged UI self-test."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parent
    env = os.environ.copy()
    windows = Path(env.get("SystemRoot", r"C:\Windows"))
    python = Path(sys.executable).resolve().parent
    # Other tools on PATH may ship incompatible DLLs with Windows system names
    # (notably Poppler/Conda's ICU). Do not let dependency analysis collect them.
    env["PATH"] = os.pathsep.join(map(str, (python, python / "Scripts",
                                           windows / "System32", windows)))
    for name in ("PYTHONPATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(name, None)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--onefile", "--windowed", "--name", "ATK-AULA-DualBattery",
               "--hidden-import", "hid"]
    for asset in ("鼠标滚轮.svg", "耳机.svg"):
        command.extend(("--add-data", f"{root / asset}{os.pathsep}."))
    command.append(str(root / "atk_tray.py"))
    result = subprocess.run(command, cwd=root, env=env)
    if result.returncode:
        return result.returncode

    report = root / "build" / "packaged-ui-check.json"
    report.unlink(missing_ok=True)
    exe = root / "dist" / "ATK-AULA-DualBattery.exe"
    result = subprocess.run([str(exe), "--self-test", str(report)], cwd=root,
                            env=env, timeout=30)
    if result.returncode or not report.is_file():
        raise RuntimeError("Packaged UI self-test failed; do not distribute this EXE.")
    print(report.read_text(encoding="utf-8"))
    print(f"Build and packaged UI self-test passed: {exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
