"""Import the original competition data into a fresh, isolated demo database."""

import argparse
import json
import tempfile
from pathlib import Path

from tax_harness.database import Database
from tax_harness.import_data import import_bundle


def main():
    parser = argparse.ArgumentParser(description="从 v1.1.0 原始材料创建全新的演示数据库")
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        bundle = args.bundle.resolve(strict=True)
        output = Path(__file__).resolve().parent.parent / "data" / "demo"
        output.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="run-", dir=output)) / "harness.sqlite3"
        result = import_bundle(Database(path), bundle)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"演示数据初始化失败：{exc}\n")
    print(json.dumps({"database": str(path), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
