"""Check the packaged file hashes and, optionally, the installed runtime."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="核对源码包与当前 Python 环境的运行代码")
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    manifest = root / "RELEASE.json"
    try:
        release = json.loads(manifest.read_text(encoding="utf-8"))
        runtime_dir = None
        if args.installed:
            spec = importlib.util.find_spec("tax_harness")
            if spec is None or spec.origin is None:
                parser.exit(1, "当前环境未安装 tax-harness\n")
            runtime_dir = Path(spec.origin).parent
        for name, expected in release["files"].items():
            if file_hash(root / name) != expected:
                parser.exit(1, f"提交文件校验失败：{name}\n")
            if runtime_dir is not None and name.startswith("src/tax_harness/"):
                runtime_file = runtime_dir / name.removeprefix("src/tax_harness/")
                if file_hash(runtime_file) != expected:
                    parser.exit(1, f"已安装的运行文件与源码包不同：{name}\n")
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"无法核对提交版本：{exc}\n")
    print(f"{release['release']} | manifest SHA-256 {file_hash(manifest)}")
    print(f"已核对 {len(release['files'])} 个提交文件")
    if runtime_dir is not None:
        print("已安装的运行代码与提交包一致")


if __name__ == "__main__":
    main()
