#!/usr/bin/env python3
"""Create and verify a revision-pinned, network-independent MinerU model bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Iterable


PIPELINE_REPO = "opendatalab/PDF-Extract-Kit-1.0"
VLM_REPO = "opendatalab/MinerU2.5-Pro-2605-1.2B"

# Keep this list aligned with mineru.utils.enum_class.ModelPath in MinerU 3.4.4.
PIPELINE_PATHS = (
    "models/Layout/PP-DocLayoutV2",
    "models/MFR/unimernet_hf_small_2503",
    "models/MFR/pp_formulanet_plus_m",
    "models/OCR/paddleocr_torch",
    "models/TabRec/SlanetPlus/slanet-plus.onnx",
    "models/TabRec/UnetStructure/unet.onnx",
    "models/TabCls/paddle_table_cls/PP-LCNet_x1_0_table_cls.onnx",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if ".cache" in relative.parts:
            continue
        yield path


def validate_required_paths(model_root: Path) -> None:
    pipeline_root = model_root / "pipeline"
    vlm_root = model_root / "vlm"
    missing: list[str] = []

    for relative_name in PIPELINE_PATHS:
        path = pipeline_root / relative_name
        if path.is_dir():
            if not any(child.is_file() and child.stat().st_size for child in path.rglob("*")):
                missing.append(f"{relative_name} (empty directory)")
        elif not path.is_file() or path.stat().st_size == 0:
            missing.append(relative_name)

    required_vlm_files = ("config.json", "model.safetensors", "tokenizer.json")
    for relative_name in required_vlm_files:
        path = vlm_root / relative_name
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(f"vlm/{relative_name}")

    if missing:
        raise RuntimeError("MinerU model bundle is incomplete: " + ", ".join(missing))


def write_config(config_path: Path, model_root: Path) -> None:
    config = {
        "models-dir": {
            "pipeline": str((model_root / "pipeline").resolve()),
            "vlm": str((model_root / "vlm").resolve()),
        },
        "model-source": "huggingface",
        "config_version": "1.3.2",
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def download_snapshot(
    snapshot_download,
    *,
    repo_id: str,
    revision: str,
    cache_dir: Path,
    allow_patterns: list[str] | None,
    retries: int,
    max_workers: int,
) -> Path:
    """Download an immutable snapshot, resuming partial files after network errors."""
    for attempt in range(1, retries + 1):
        try:
            return Path(
                snapshot_download(
                    repo_id=repo_id,
                    revision=revision,
                    cache_dir=cache_dir,
                    allow_patterns=allow_patterns,
                    max_workers=max_workers,
                )
            )
        except Exception as error:
            if attempt == retries:
                raise
            delay = min(5 * attempt, 60)
            print(
                f"Snapshot {repo_id}@{revision} failed on attempt "
                f"{attempt}/{retries}: {error!r}. Resuming in {delay}s.",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)

    raise AssertionError("unreachable")


def build_manifest(
    model_root: Path,
    manifest_path: Path,
    pipeline_revision: str,
    vlm_revision: str,
) -> None:
    files = {}
    for path in model_files(model_root):
        relative_name = path.relative_to(model_root).as_posix()
        files[relative_name] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }

    manifest = {
        "schema": 1,
        "mineru_version": "3.4.4",
        "repositories": {
            PIPELINE_REPO: pipeline_revision,
            VLM_REPO: vlm_revision,
        },
        "files": files,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def download(args: argparse.Namespace) -> None:
    from huggingface_hub import snapshot_download

    model_root = args.model_root.resolve()
    pipeline_root = model_root / "pipeline"
    vlm_root = model_root / "vlm"
    hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    cache_dir = hf_home / "hub"
    cache_dir.mkdir(parents=True, exist_ok=True)

    allow_patterns = []
    for relative_name in PIPELINE_PATHS:
        allow_patterns.extend((relative_name, f"{relative_name}/*"))

    pipeline_snapshot = download_snapshot(
        snapshot_download,
        repo_id=PIPELINE_REPO,
        revision=args.pipeline_revision,
        cache_dir=cache_dir,
        allow_patterns=allow_patterns,
        retries=args.download_retries,
        max_workers=args.max_workers,
    )
    vlm_snapshot = download_snapshot(
        snapshot_download,
        repo_id=VLM_REPO,
        revision=args.vlm_revision,
        cache_dir=cache_dir,
        allow_patterns=None,
        retries=args.download_retries,
        max_workers=args.max_workers,
    )

    # Copy dereferenced snapshot files out of the persistent build cache. The
    # final image never depends on Hugging Face's symlink/cache layout.
    shutil.rmtree(model_root, ignore_errors=True)
    model_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(pipeline_snapshot, pipeline_root, symlinks=False)
    shutil.copytree(vlm_snapshot, vlm_root, symlinks=False)

    validate_required_paths(model_root)
    write_config(args.config, model_root)
    build_manifest(
        model_root,
        args.manifest,
        args.pipeline_revision,
        args.vlm_revision,
    )


def verify(args: argparse.Namespace) -> None:
    model_root = args.model_root.resolve()
    validate_required_paths(model_root)

    config = json.loads(args.config.read_text(encoding="utf-8"))
    expected_dirs = {
        "pipeline": str(model_root / "pipeline"),
        "vlm": str(model_root / "vlm"),
    }
    if config.get("models-dir") != expected_dirs:
        raise RuntimeError(
            f"MinerU config has wrong model paths: {config.get('models-dir')!r}"
        )

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected_repositories = {
        PIPELINE_REPO: args.pipeline_revision,
        VLM_REPO: args.vlm_revision,
    }
    if manifest.get("repositories") != expected_repositories:
        raise RuntimeError("MinerU manifest repository revisions do not match the image")

    manifest_files = manifest.get("files", {})
    if not manifest_files:
        raise RuntimeError("MinerU model manifest is empty")

    for relative_name, metadata in manifest_files.items():
        path = model_root / relative_name
        if not path.is_file():
            raise RuntimeError(f"MinerU model file is missing: {relative_name}")
        if path.stat().st_size != metadata["bytes"]:
            raise RuntimeError(f"MinerU model file size changed: {relative_name}")
        if args.hashes and sha256_file(path) != metadata["sha256"]:
            raise RuntimeError(f"MinerU model checksum changed: {relative_name}")

    print(
        f"Verified {len(manifest_files)} MinerU model files "
        f"({'checksums' if args.hashes else 'sizes'})."
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("command", choices=("download", "verify"))
    result.add_argument("--model-root", type=Path, default=Path("/opt/mineru/models"))
    result.add_argument("--config", type=Path, default=Path("/opt/mineru/mineru.json"))
    result.add_argument(
        "--manifest", type=Path, default=Path("/opt/mineru/model-manifest.json")
    )
    result.add_argument("--pipeline-revision", required=True)
    result.add_argument("--vlm-revision", required=True)
    result.add_argument("--download-retries", type=int, default=100)
    result.add_argument("--max-workers", type=int, default=2)
    result.add_argument("--hashes", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    os.umask(0o022)
    if args.command == "download":
        download(args)
    else:
        verify(args)


if __name__ == "__main__":
    main()
