#!/usr/bin/env python3
"""生成 / 校验 hydromarket-index 的 index.json。

设计要点（都是踩过的坑，别顺手改掉）：

* **索引是生成物**，人只维护 `plugins/<id>/<version>/` 下的 `.hpk` + `meta.json`；
  改完包必须跑一次本脚本（或让 CI 跑），否则客户端拿到的 sha256 与文件对不上。
* **元数据单一真相在包里**：`id/name/author/version/runtime/permissions/minHydroLink`
  全部从 `.hpk` 内的 `manifest.json`（扁平形状）解析，`meta.json` 里的同名字段如果写了
  且不一致 → 直接失败。避免「索引说有 description、包里没有」这类漂移。
* **只有第三方市场元数据放 meta.json**：`description`/`icon` 必填，`category` 只能是
  game/utility/other，截图与关键词可选。
* **`generated_at` 取被索引文件里最新的 mtime**，不是当前时间：否则每次跑都产生 diff，
  CI 的「索引是否最新」检查会一直红。
* **索引里每个 id 只出最新一版**，但仓库里所有版本的文件都保留（路径不可变，客户端
  可以 pin 到精确版本）。版本比较与客户端 `PluginStore.compareVersions` / 桌面端
  `store.CompareVersions` 同规则：缺段按 0、预发布低于正式。
* 只用标准库，CI 零依赖。

用法：
    python3 scripts/build_index.py            # 写 index.json
    python3 scripts/build_index.py --check    # 只校验：索引不是最新就退出码 1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

SCHEMA = 1
CATEGORIES = ("game", "utility", "other")

DESCRIPTION_MAX = 300
ICON_MAX_BYTES = 256 * 1024
SCREENSHOT_MAX_BYTES = 2 * 1024 * 1024
SCREENSHOTS_MAX = 5
KEYWORDS_MAX = 8

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
INDEX_PATH = REPO_ROOT / "index.json"


class ValidationError(Exception):
    """一条条攒起来，最后一起报（CI 里一次看全，别一行一行刷）。"""


def _fail(errors: list[str], msg: str) -> None:
    errors.append(msg)


# ---------- 版本比较（与两端客户端同规则） ----------

def _ver_parts(v: str) -> tuple[list[int], str, bool]:
    """拆成 (数字段, 预发布串, 是否预发布)。`1.0` == `1.0.0`，`1.0.0-beta` < `1.0.0`。"""
    v = (v or "").strip().lstrip("vV")
    v = v.split("+", 1)[0]          # build metadata（+20260901）不参与比较
    pre = ""
    if "-" in v:
        v, _, pre = v.partition("-")
    nums = []
    for part in v.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return nums, pre, bool(pre)


def compare_versions(a: str, b: str) -> int:
    """a<b → -1，a==b → 0，a>b → 1。"""
    na, pa, ha = _ver_parts(a)
    nb, pb, hb = _ver_parts(b)
    width = max(len(na), len(nb))
    for i in range(width):
        x = na[i] if i < len(na) else 0
        y = nb[i] if i < len(nb) else 0
        if x != y:
            return -1 if x < y else 1
    if ha != hb:
        return -1 if ha else 1
    if pa != pb:
        if not pa:
            return 1
        if not pb:
            return -1
        return -1 if pa < pb else 1
    return 0


# ---------- 读取 ----------

def read_manifest(hpk: Path) -> dict:
    """读 .hpk 内的扁平 manifest.json。"""
    try:
        with zipfile.ZipFile(hpk) as zf:
            with zf.open("manifest.json") as fh:
                return json.loads(fh.read().decode("utf-8"))
    except KeyError:
        raise ValidationError(f"{hpk.relative_to(REPO_ROOT)}: 包内没有 manifest.json")
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError(f"{hpk.relative_to(REPO_ROOT)}: 包读不出来（{exc}）")


def runtime_of(manifest: dict) -> str:
    """manifest.runtime 优先，否则按入口后缀猜（js / wasm）。"""
    declared = (manifest.get("runtime") or "").strip()
    if declared:
        return declared
    entry = (manifest.get("entry") or "").strip()
    if entry.endswith(".wasm"):
        return "wasm"
    return "js"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_png(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(8) == b"\x89PNG\r\n\x1a\n"


# ---------- 校验 + 建条目 ----------

def build_entry(pkg_dir: Path, errors: list[str]) -> dict | None:
    """校验一个版本目录，成功返回索引条目。返回 None = 校验不过，已往 errors 里记。"""
    rel_dir = pkg_dir.relative_to(REPO_ROOT).as_posix()
    hpks = sorted(pkg_dir.glob("*.hpk"))
    if not hpks:
        _fail(errors, f"{rel_dir}: 目录里没有 .hpk")
        return None
    if len(hpks) > 1:
        _fail(errors, f"{rel_dir}: 一个版本目录里只能有一个 .hpk（发现 {len(hpks)} 个）")
        return None
    hpk = hpks[0]

    meta_path = pkg_dir / "meta.json"
    if not meta_path.is_file():
        _fail(errors, f"{rel_dir}: 缺 meta.json（description 与 icon 是必填的市场元数据）")
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _fail(errors, f"{rel_dir}/meta.json: 解析失败（{exc}）")
        return None

    manifest = read_manifest(hpk)
    pid = str(manifest.get("id") or "").strip()
    version = str(manifest.get("version") or "").strip()
    name = str(manifest.get("name") or "").strip()

    # 目录名必须与包内清单一致：plugins/<id>/<version>/
    if pid != pkg_dir.parent.name:
        _fail(errors, f"{rel_dir}: 目录里的 id 应为 {pid!r}（包内 manifest.id）")
        return None
    if version != pkg_dir.name:
        _fail(errors, f"{rel_dir}: 目录名应为版本 {version!r}（包内 manifest.version）")
        return None
    if not pid or not version or not name:
        _fail(errors, f"{rel_dir}: manifest 缺 id / version / name")
        return None
    if hpk.name != f"{pid}.hpk":
        _fail(errors, f"{rel_dir}: 包名应为 {pid}.hpk（现在是 {hpk.name}）")

    # meta 里如果重复写了包内字段，必须一致（不允许 meta 覆盖包）
    for field, value in (("id", pid), ("version", version), ("name", name)):
        if field in meta and str(meta[field]).strip() != value:
            _fail(errors, f"{rel_dir}/meta.json: {field}={meta[field]!r} 与包内 {value!r} 不一致")
            return None

    description = str(meta.get("description") or "").strip()
    if not description:
        _fail(errors, f"{rel_dir}/meta.json: description 必填（说清这个插件在手表上干什么）")
        return None
    if len(description) > DESCRIPTION_MAX:
        _fail(errors, f"{rel_dir}/meta.json: description 超 {DESCRIPTION_MAX} 字（现在 {len(description)}）")
        return None

    category = str(meta.get("category") or "").strip()
    if category not in CATEGORIES:
        _fail(errors, f"{rel_dir}/meta.json: category 只能是 {'/'.join(CATEGORIES)}（现在是 {category!r}）")
        return None

    icon_ref = str(meta.get("icon") or "").strip()
    if not icon_ref:
        _fail(errors, f"{rel_dir}/meta.json: icon 必填（相对本目录的文件名，如 icon.png）")
        return None
    icon_path = (pkg_dir / icon_ref).resolve()
    if not str(icon_path).startswith(str(REPO_ROOT.resolve())):
        _fail(errors, f"{rel_dir}/meta.json: icon 不能指向目录外（{icon_ref}）")
        return None
    if not icon_path.is_file():
        _fail(errors, f"{rel_dir}/meta.json: icon 文件不存在（{icon_ref}）")
        return None
    if icon_path.stat().st_size > ICON_MAX_BYTES:
        _fail(errors, f"{rel_dir}/{icon_ref}: 图标超过 {ICON_MAX_BYTES // 1024}KB")
        return None
    if not _is_png(icon_path):
        _fail(errors, f"{rel_dir}/{icon_ref}: 图标必须是 PNG")

    screenshots: list[str] = []
    raw_shots = meta.get("screenshots") or []
    if not isinstance(raw_shots, list):
        _fail(errors, f"{rel_dir}/meta.json: screenshots 必须是数组")
        return None
    if len(raw_shots) > SCREENSHOTS_MAX:
        _fail(errors, f"{rel_dir}/meta.json: 截图最多 {SCREENSHOTS_MAX} 张")
        return None
    for ref in raw_shots:
        ref = str(ref).strip()
        shot = (pkg_dir / ref).resolve()
        if not str(shot).startswith(str(REPO_ROOT.resolve())) or not shot.is_file():
            _fail(errors, f"{rel_dir}/meta.json: 截图不存在（{ref}）")
            return None
        if shot.stat().st_size > SCREENSHOT_MAX_BYTES:
            _fail(errors, f"{rel_dir}/{ref}: 截图超过 {SCREENSHOT_MAX_BYTES // 1024 // 1024}MB")
            return None
        screenshots.append(shot.relative_to(REPO_ROOT).as_posix())

    keywords = [str(k).strip() for k in (meta.get("keywords") or []) if str(k).strip()]
    if len(keywords) > KEYWORDS_MAX:
        _fail(errors, f"{rel_dir}/meta.json: keywords 最多 {KEYWORDS_MAX} 个")
        return None

    entry = {
        "id": pid,
        "name": name,
        "author": str(manifest.get("author") or "").strip(),
        "version": version,
        "runtime": runtime_of(manifest),
        "entry": str(manifest.get("entry") or "").strip(),
        "min_hydrolink": str(manifest.get("minHydroLink") or "").strip(),
        "permissions": list(manifest.get("permissions") or []),
        "description": description,
        "category": category,
        "icon": icon_path.relative_to(REPO_ROOT).as_posix(),
        "url": hpk.relative_to(REPO_ROOT).as_posix(),
        "sha256": sha256_of(hpk),
        "size": hpk.stat().st_size,
    }
    if manifest.get("url"):
        entry["homepage"] = str(manifest["url"]).strip()
    if meta.get("homepage"):
        entry["homepage"] = str(meta["homepage"]).strip()
    if keywords:
        entry["keywords"] = keywords
    if screenshots:
        entry["screenshots"] = screenshots
    return entry


def collect() -> tuple[dict, list[str]]:
    """扫描 plugins/，返回 (索引字典, 错误列表)。"""
    errors: list[str] = []
    by_id: dict[str, list[dict]] = {}

    if not PLUGINS_DIR.is_dir():
        _fail(errors, "缺少 plugins/ 目录")
        return {"schema": SCHEMA, "generated_at": "", "plugins": []}, errors

    for pkg_dir in sorted(p for p in PLUGINS_DIR.glob("*/*") if p.is_dir()):
        entry = build_entry(pkg_dir, errors)
        if entry is not None:
            by_id.setdefault(entry["id"], []).append(entry)

    latest: list[dict] = []
    newest_mtime = 0.0
    for pid, entries in sorted(by_id.items()):
        newest = entries[0]
        for candidate in entries[1:]:
            if compare_versions(candidate["version"], newest["version"]) > 0:
                newest = candidate
        latest.append(newest)
        for entry in entries:
            newest_mtime = max(newest_mtime, (REPO_ROOT / entry["url"]).stat().st_mtime)

    import datetime

    stamp = (
        datetime.datetime.fromtimestamp(newest_mtime, datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
        if newest_mtime
        else ""
    )
    return {"schema": SCHEMA, "generated_at": stamp, "plugins": latest}, errors


def render(index: dict) -> str:
    return json.dumps(index, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="生成/校验 hydromarket-index 的 index.json")
    parser.add_argument("--check", action="store_true", help="只校验：索引不是最新就退出码 1")
    args = parser.parse_args(argv)

    index, errors = collect()
    if errors:
        print("校验不通过：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    text = render(index)
    current = INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.is_file() else None

    if args.check:
        if current != text:
            print("index.json 与 plugins/ 不同步，跑 python3 scripts/build_index.py 重新生成", file=sys.stderr)
            return 1
        print(f"index.json 已是最新（{len(index['plugins'])} 个插件）")
        return 0

    if current == text:
        print(f"index.json 无变化（{len(index['plugins'])} 个插件）")
        return 0
    INDEX_PATH.write_text(text, encoding="utf-8")
    print(f"已写入 index.json（{len(index['plugins'])} 个插件）")
    for plugin in index["plugins"]:
        print(f"  - {plugin['id']} v{plugin['version']} ({plugin['runtime']}, {plugin['category']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
