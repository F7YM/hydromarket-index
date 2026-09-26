#!/usr/bin/env python3
"""校验仓库内容（CI 里显式跑一步，报错信息比 build --check 更直白）。

两件事：
1. plugins/ 下每个版本目录都过一遍 build_index 的校验（meta 必填项、category 受控表、
   图标/截图存在与大小、包内 manifest 与目录名一致……）。
2. index.json 存在且与 plugins/ 同步（等价于 build_index.py --check，单独一个入口
   是为了让 CI 的失败原因一眼能看懂是「包有问题」还是「索引没重建」）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_index import INDEX_PATH, collect, render  # noqa: E402


def main() -> int:
    index, errors = collect()
    if errors:
        print("包/元数据校验不通过：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    if not INDEX_PATH.is_file():
        print("缺 index.json：跑 python3 scripts/build_index.py 生成", file=sys.stderr)
        return 1

    current = INDEX_PATH.read_text(encoding="utf-8")
    if current != render(index):
        print(
            "index.json 与 plugins/ 不同步：跑 python3 scripts/build_index.py 重新生成后提交",
            file=sys.stderr,
        )
        return 1

    print(f"校验通过：{len(index['plugins'])} 个插件，索引已同步")
    for plugin in index["plugins"]:
        print(
            f"  - {plugin['id']} v{plugin['version']} · {plugin['name']} · "
            f"{plugin['runtime']} · {plugin['category']} · {plugin['size']}B"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
