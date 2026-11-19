"""Provision only dependency venv; no job data or credentials belong here."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / "requirements.txt"

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def identity():
    if sys.version_info[:2] != (3, 13) or platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("BOOT_PLATFORM")
    return {"schema": 2, "python": sys.version, "executable": str(Path(sys.executable).resolve()),
            "platform": platform.platform(), "image": os.getenv("ImageVersion", "local"),
            "lock": digest(LOCK), "bootstrap": digest(Path(__file__))}

def tree(root):
    # venv interpreter links are checked too; never execute the cached interpreter before checking.
    result = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if rel == ".ready.json":
            continue
        if path.is_symlink():
            result[rel] = "link:" + os.readlink(path)
        elif path.is_file():
            result[rel] = digest(path)
    return result

def main():
    start = time.monotonic()
    try:
        expected = identity()
        if "--identity" in sys.argv:
            print(hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest())
            return 0
        target = Path(os.environ["TTS_DEP_DIR"]).absolute()
        # Refuse dangerous targets or job/repository roots. Dedicated venv only.
        if target.name != "daka-tts152-deps-v2" or target.is_symlink():
            raise RuntimeError("BOOT_TARGET")
        ready = target / ".ready.json"
        valid = False
        try:
            state = json.loads(ready.read_text())
            valid = state["identity"] == expected and state["files"] == tree(target)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if valid:
            print("DEPS_READY mode=warm install=skipped")
            return 0
        print("DEPS_BUILD reason=missing_or_invalid")
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        def run(args, code):
            completed = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
            if completed.returncode:
                print(f"DEPS_ERROR code={code} exit={completed.returncode}")
                raise RuntimeError("BOOT_REPORTED")
        run([sys.executable, "-I", "-B", "-m", "venv", str(target)], "BOOT_VENV")
        python = str(target / "bin/python")
        run([python, "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
             "--no-cache-dir", "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
             "--require-hashes", "--no-deps", "--no-compile", "-r", str(LOCK)], "BOOT_INSTALL")
        run([python, "-I", "-B", "-m", "pip", "check"], "BOOT_DEPENDENCIES")
        run([python, "-I", "-B", "-c", "import edge_tts,miniaudio; assert edge_tts.__version__ == '7.3.1'"], "BOOT_IMPORT")
        ready.write_text(json.dumps({"identity": expected, "files": tree(target)}, sort_keys=True))
        print(f"DEPS_READY mode=cold seconds={time.monotonic()-start:.1f}")
        return 0
    except Exception as error:
        known = {"BOOT_PLATFORM", "BOOT_TARGET", "BOOT_REPORTED"}
        code = error.args[0] if type(error) is RuntimeError and error.args and error.args[0] in known else "BOOT_FAILED"
        if code != "BOOT_REPORTED":
            print("DEPS_ERROR code=" + code)
        return 1

if __name__ == "__main__":
    sys.exit(main())
