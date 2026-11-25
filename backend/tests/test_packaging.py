import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

BACKEND = Path(__file__).resolve().parent.parent
SENTINEL = "CHAINLIT_WHEEL_SENTINEL_DO_NOT_SHIP_7f3c"


def test_wheel_excludes_local_and_sensitive_package_files(tmp_path):
    backend_copy = tmp_path / "backend"
    backend_copy.mkdir()
    for filename in ("pyproject.toml", "build.py", "README.md"):
        shutil.copy2(BACKEND / filename, backend_copy / filename)
    shutil.copytree(BACKEND / "chainlit", backend_copy / "chainlit")

    package = backend_copy / "chainlit"
    sentinels = {
        ".env": SENTINEL,
        ".env.local": SENTINEL,
        ".chainlit/config.toml": SENTINEL,
        ".uploads/upload.txt": SENTINEL,
        "local-notes.md": SENTINEL,
        "local-private.key": SENTINEL,
        "local-certificate.pem": SENTINEL,
        "security-audit/report.md": SENTINEL,
        "sample/eval.md": SENTINEL,
        "sample/.chainlit/config.toml": SENTINEL,
        "sample/.uploads/upload.txt": SENTINEL,
        "sample/whats_new_after_2_12/notes.md": SENTINEL,
        "__pycache__/local.cpython-313.pyc": SENTINEL,
        "local.pyc": SENTINEL,
        "frontend/dist/.env": SENTINEL,
        "frontend/dist/.uploads/upload.txt": SENTINEL,
        "frontend/dist/__pycache__/local.pyc": SENTINEL,
        "frontend/dist/private.key": SENTINEL,
        "frontend/dist/index.html": "<html>frontend sentinel</html>",
        "frontend/dist/assets/app.js": "console.log('frontend sentinel');",
        "frontend/dist/assets/app.js.map": '{"sourcesContent":["sentinel"]}',
        "copilot/dist/copilot.js": "console.log('copilot sentinel');",
        "copilot/dist/copilot.js.map": '{"sourcesContent":["sentinel"]}',
    }
    for relative_path, content in sentinels.items():
        path = package / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    distribution = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "hatchling",
            "build",
            "-t",
            "wheel",
            "-d",
            str(distribution),
        ],
        cwd=backend_copy,
        check=True,
        capture_output=True,
        text=True,
    )

    wheel = next(distribution.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        package_names = {name for name in names if name.startswith("chainlit/")}

    required = {
        "chainlit/__init__.py",
        "chainlit/version.py",
        "chainlit/py.typed",
        "chainlit/translations/en-US.json",
        "chainlit/sample/hello.py",
        "chainlit/sample/skills/summarize-changelog/SKILL.md",
        "chainlit/sample/skills/write-release-notes.md",
        "chainlit/frontend/dist/index.html",
        "chainlit/frontend/dist/assets/app.js",
        "chainlit/copilot/dist/copilot.js",
    }
    assert required <= package_names, required - package_names

    forbidden = []
    private_suffixes = {".key", ".pem", ".crt", ".cer", ".p12", ".pfx"}
    for name in package_names:
        path = PurePosixPath(name)
        parts = path.parts[1:]

        if (
            any(part.startswith(".") for part in parts)
            or any(part in {".uploads", "__pycache__"} for part in parts)
            or path.suffix.lower() in {".pyc", ".pyo", ".pyd", ".map"}
            or path.suffix.lower() in private_suffixes
            or "sample/whats_new_after_2_12/" in name
            or name == "chainlit/sample/eval.md"
            or "/security-audit/" in name
        ):
            forbidden.append(name)

    assert not forbidden, f"Unexpected package files: {sorted(forbidden)}"

    with zipfile.ZipFile(wheel) as archive:
        assert all(
            SENTINEL.encode() not in archive.read(name)
            for name in package_names
            if not name.endswith("/")
        )
