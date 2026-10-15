"""Validate and export the submission source, excluding local kit/data/secrets."""
import argparse
import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    ".env.example", ".gitignore", ".github/workflows/ci.yml",
    "app/__init__.py", "app/main.py", "requirements.txt", "requirements-dev.txt",
    "README.md", "REVIEW.md", "DECISIONS.md", "scripts/dev.ps1",
    "scripts/submission.py", "tests/test_api.py", "tests/test_submission.py", "tests/benchmark.py",
)
FORBIDDEN_DIRECTORIES = {".venv", "venv", ".local", "_reference", "sample_data", "__pycache__", ".git"}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"mongodb(?:\+srv)?://[^\s/:@]+:[^\s@]+@"),
)


def forbidden_path(relative):
    path = PurePosixPath(relative.replace("\\", "/"))
    return (path.is_absolute() or ".." in path.parts
            or bool(set(path.parts) & FORBIDDEN_DIRECTORIES)
            or path.name.lower() == "dockerfile"
            or (path.name.startswith(".env") and path.name != ".env.example")
            or path.suffix.lower() in {".zip", ".bson", ".dump", ".db", ".pyc"})


def validate(root=ROOT):
    root = root.resolve()
    for relative in SOURCE_FILES:
        path = root / relative
        if forbidden_path(relative) or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError(f"Missing or invalid submission source: {relative}")
        content = path.read_text(encoding="utf-8")
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            # Report the filename only; never print a suspected secret.
            raise ValueError(f"Possible credential in submission source: {relative}")
    decisions = (root / "DECISIONS.md").read_text(encoding="utf-8").split()
    if not 150 <= len(decisions) <= 200:
        raise ValueError("DECISIONS.md must be approximately 150–200 words; current draft is outside that range")
    # Unlike an archive-only allowlist, this also catches forbidden files already
    # tracked in Git, even if .gitignore would now hide them from new additions.
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=root, capture_output=True, check=True)
    public_paths = result.stdout.decode("utf-8").split("\0")
    violations = [name for name in public_paths if name and forbidden_path(name)]
    if violations:
        raise ValueError("Forbidden files in Git's publishable set: " + ", ".join(violations))
    return SOURCE_FILES


def build_archive(root=ROOT):
    files = validate(root)
    target = root / ".local" / "submission" / "hrone-attendance-api.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for relative in files:
            info = ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (root / relative).read_bytes())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    return target, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate only; do not create an archive")
    args = parser.parse_args()
    try:
        if args.check:
            print(f"Submission check passed: {len(validate())} source files; no prohibited publishable files found.")
        else:
            target, digest = build_archive()
            print(f"Source archive: {target}\nSHA256: {digest}")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Submission check failed: {error}\n")


if __name__ == "__main__":
    main()
