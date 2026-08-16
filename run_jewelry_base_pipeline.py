"""旧通用入口：只做参数防护并委托正式编排器。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Sequence


UNSAFE_LEGACY_OPTIONS = {
    "--retry",
    "--authorization-reference",
    "--allow-size-override",
    "--append-version",
}


def _load_workflow_main():
    script = (
        Path(__file__).resolve().parent
        / "skills"
        / "jewelry-white-background"
        / "scripts"
        / "run_white_background_workflow.py"
    )
    spec = importlib.util.spec_from_file_location("_jewelry_formal_workflow_entry", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载正式编排器：{script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module.main


def _reject_unsafe_legacy_options(argv: Sequence[str]) -> None:
    blocked = sorted(UNSAFE_LEGACY_OPTIONS.intersection(argv))
    if blocked:
        raise SystemExit(f"旧参数无法安全映射到正式工作流：{', '.join(blocked)}")


workflow_main = _load_workflow_main()


def main(argv: Sequence[str] | None = None) -> int:
    forwarded = list(sys.argv[1:] if argv is None else argv)
    _reject_unsafe_legacy_options(forwarded)
    return workflow_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
