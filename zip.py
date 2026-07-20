from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import pathspec


ROOT = Path.cwd()
ZIP_SCRIPT = Path(__file__).resolve()
ZIP_PATH = ROOT / f"{ROOT.name}.zip"
IGNORE_FILES = [".gitignore", ".dockerignore"]


# Loads .gitignore and .dockerignore patterns using git-style matching.
def load_ignore_spec():
    patterns = []

    for ignore_file in IGNORE_FILES:
        path = ROOT / ignore_file
        if path.exists():
            patterns.extend(path.read_text(encoding="utf-8").splitlines())

    extra_ignores = [
        ZIP_SCRIPT.relative_to(ROOT).as_posix(),
        ZIP_PATH.relative_to(ROOT).as_posix(),
    ]

    patterns.extend(extra_ignores)

    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


# Creates a zip of the current folder, skipping ignored files.
def make_zip():
    ignore_spec = load_ignore_spec()

    with ZipFile(ZIP_PATH, "w", ZIP_DEFLATED) as zip_file:
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue

            relative_path = path.relative_to(ROOT).as_posix()

            if ignore_spec.match_file(relative_path):
                continue

            zip_file.write(path, relative_path)

    print(f"Created {ZIP_PATH}")


if __name__ == "__main__":
    make_zip()
