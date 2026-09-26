# hydromarket-index

HydroLink 插件市场的**索引仓**：只放索引与插件发布产物，不放插件源码（源码在各插件自己的仓库）。

客户端（HydroLink 安卓端 / 桌面端）直接读这里的 `index.json` 与 `.hpk`，**服务端不参与、不加任何 HTTP 路由**。

## 目录

```
index.json                              # 生成物，勿手改（由 scripts/build_index.py 产出）
plugins/<id>/<version>/
  ├── <id>.hpk                          # 插件包（zip）
  ├── icon.png                          # 图标，必填，PNG ≤256KB
  ├── screenshot-*.png                  # 截图，可选，≤5 张、每张 ≤2MB
  └── meta.json                         # 市场元数据（见下）
scripts/build_index.py                  # 扫描 + 校验 + 生成索引
scripts/validate.py                     # 只校验（本地/CI 用）
```

## 发布一个插件版本

1. 在 `plugins/<id>/<version>/` 放 `<id>.hpk`（版本路径**不可变**：升版就加新目录，旧的留着，客户端可 pin 精确版本）
2. 同目录放 `icon.png` 与 `meta.json`（`description`、`icon`、`category` 必填）
3. 提交推送 → CI 重建并提交 `index.json` → raw 地址立即生效

本地先自查一遍：

```bash
python3 scripts/build_index.py     # 重建索引（有 diff 就写回）
python3 scripts/validate.py        # 只校验：包不合规或索引没同步都退出码非 0
```

## `meta.json`

```json
{
  "description": "必填，≤300 字：说清这个插件在手表上干什么",
  "icon": "icon.png",
  "category": "game | utility | other",
  "keywords": ["可选", "≤8 个"],
  "screenshots": ["s1.png"],
  "homepage": "https://…"
}
```

`id` / `name` / `version` / `author` / `permissions` / `min_hydrolink` **不在 meta 里写**——一律从 `.hpk` 内的 `manifest.json` 解析（单一真相）。meta 里如果写了这些字段，必须与包内一致，否则校验失败。

## 客户端怎么取

索引与包都用「仓库内相对路径 + 可切换的基址」拼接，基址候选：

| 候选 | 基址 |
|---|---|
| 直连（首选） | `https://raw.githubusercontent.com/F7YM/hydromarket-index/main` |
| gh-proxy | `https://gh-proxy.org/https://raw.githubusercontent.com/F7YM/hydromarket-index/main` |
| jsDelivr | `https://cdn.jsdelivr.net/gh/F7YM/hydromarket-index@main` |
| 自定义 | 用户在设置里手填 |

`icon` / `screenshots` / `url` 在索引里都是**相对路径**，换基址不用改索引。下载 `.hpk` 后必须核对 `sha256` 与 `size`，不符直接拒绝安装。
