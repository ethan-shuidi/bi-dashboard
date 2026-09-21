from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from release_guards import run_all_checks


def main() -> int:
    errors = run_all_checks()
    if errors:
        print("看板发布检查失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("看板发布检查通过：短期写权限、运行配置、接口清单与构建产物防线均有效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
