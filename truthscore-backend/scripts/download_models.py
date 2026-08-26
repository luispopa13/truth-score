"""Download local ML weights into ./models (~640 MB total).

GitHub blocks pushes containing files > 100 MB (the largest local model
file is ~449 MB), so model weights live outside version control and are
fetched at deployment time instead:

    python scripts/download_models.py

Run this once before starting the API in a fresh environment (Dockerfile /
Render build step / local dev). Safe to re-run: already-present models are
skipped via marker checks.
"""
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

ROOT = Path(__file__).resolve().parents[1]

REPOS = {
    "models/embed_en":     "sentence-transformers/all-MiniLM-L6-v2",
    "models/embed_multi":  "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "models/crossencoder": "cross-encoder/ms-marco-MiniLM-L-6-v2",
}

ALLOW = ["*.json", "*.txt", "*.model", "*.safetensors",
         "modules.json", "1_Pooling/*"]
IGNORE = ["onnx/*", "openvino/*", "*.onnx", "*.msgpack", "*.h5",
          "rust_model.ot", "flax_model.*", "*_openvino*"]


def main() -> int:
    failures = 0
    for rel, repo in REPOS.items():
        dest = ROOT / rel
        if (dest / "config.json").exists() and list(dest.glob("*.safetensors")):
            print(f"[SKIP] {rel} already present")
            continue
        print(f"[GET ] {repo} -> {rel}")
        try:
            snapshot_download(
                repo_id=repo,
                local_dir=str(dest),
                allow_patterns=ALLOW,
                ignore_patterns=IGNORE,
            )
            print(f"[ OK ] {rel}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {repo}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())