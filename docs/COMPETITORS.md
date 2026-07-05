# 竞品对比

Skillmesh 不与任何竞品正面竞争。本文档客观呈现各竞品的能力，让用户自选最适合自己的工具。

数据来源：2026-07-04 实读各竞品 README。如有变化欢迎 PR 更新。

## 直接竞品

| repo | star | 语言 | 形态 | 适合谁 |
| --- | --- | --- | --- | --- |
| `qufei1993/skills-hub` | ★1101 | Rust + Tauri | GUI 桌面 app | 想要现成 GUI、不写 config、46 个内置 Agent 全平台 |
| `Duducoco/skillstash` | ★5 | TypeScript | CLI（npm） | 习惯 git workflow、跨平台含 Windows、要 copy 模式 |
| `kamusis/axon-cli` | ★3 | Go | CLI（binary） | 重度 symlink、要 AI security audit、要语义搜索 |
| `skillmesh` | - | Python std-lib | CLI | 用 iCloud/国产网盘不走 git、重视 lifecycle 完整性、要零依赖可审计 |

## 功能对比矩阵

| 能力 | qufei1993 | skillstash | axon-cli | skillmesh |
| --- | :-: | :-: | :-: | :-: |
| 形态 | GUI（Tauri） | CLI（npm） | CLI（Go binary） | CLI（Python std-lib） |
| Windows 支持 | ✅ | ✅ | ✅ | ✅（0.2+） |
| macOS 支持 | ✅ | ✅ | ✅ | ✅ |
| Linux 支持 | ✅ | ✅ | ✅ | ✅ |
| Agent 数量 | 46 内置 | auto-detect + 自定义 | 20 内置 + 自定义 | config 声明（任意） |
| 分发方式 | symlink 优先 + copy fallback | copy only | symlink | symlink / junction / managed copy |
| 多机 sync | Git URL import（非实时） | git remote + 三路合并 | git sync | 任意 sync 后端 |
| **非 git sync 后端** | ❌ | ❌ | ❌ | ✅（iCloud/Dropbox/Syncthing/坚果云/百度/夸克/阿里云盘） |
| Peer-write 并发（双机同时改） | ❌ | ⚠️（靠 git merge） | ⚠️（靠 git merge） | ✅（event log + Lamport + CAS） |
| CAS blob 存储 | ❌ | ❌ | ❌ | ✅ |
| Genesis snapshot + deterministic replay | ❌ | ❌ | ❌ | ✅ |
| 完整 lifecycle 状态机 | ❌（基础 install/uninstall） | ❌（install/link/unlink） | ❌（link/unlink/rollback） | ✅（detach/attach/uninstall/forget/purge/gc） |
| 全量 backup/rollback | ❓ | ❌ | ✅（per-skill 或 whole hub） | ✅ |
| Tags / scope 管理 | ✅ | ❌ | ❌ | ❌（v1 不做） |
| Skill explore / search 市场 | ✅（curated featured） | ❓ | ✅（keyword + semantic） | ❌（v1 不做） |
| AI security audit | ❌ | ❌ | ✅ | ❌ |
| 文件级 fanout（一文件→多目标名） | ❓ | ❌ | ✅（CLAUDE.md/AGENTS.md/GEMINI.md ← global_rules.md） | ❌（v1 不做） |
| 无运行时依赖 | ✅（Rust binary） | ❌（需 Node runtime） | ✅（Go binary） | ✅（需 Python，macOS/Linux 自带） |
| 单脚本可审计（无 build toolchain） | ❌（cargo） | ❌（tsc/npm） | ❌（go build） | ✅（直接改 .py） |
| TUI 交互模式 | N/A（GUI） | ✅（默认进 TUI） | ❌ | ❌ |
| ClawHub / GitHub 安装源 | ✅（Git URL） | ✅（clawhub: + GitHub + local） | ✅（vendor sync） | ❌（v1 仅本地） |

## 各竞品优势

### qufei1993/skills-hub

- **46 个内置 Agent**：覆盖主流 agent，无需配置即可识别
- **GUI 桌面 app**：可视化浏览、tags 管理、Markdown 渲染
- **全平台**：macOS / Windows / Linux
- **curated featured skills**：内置精选 skill 市场
- **多语言文档**

适合：不想写 config、想要 GUI、用 Windows、需要 46 个 Agent 即装即用的用户。

### Duducoco/skillstash

- **三路合并冲突解决**：基于 `updatedAt` 的智能合并
- **copy by default**：Windows 友好，避免 symlink 权限问题
- **agent auto-detect + 自定义**：自动扫描已安装的 agent
- **local-first, remote-optional**：可纯本地用，git 同步可选
- **多设备 sync** 是显式卖点
- **ClawHub / GitHub / local 多源**

适合：习惯 git workflow、要 Windows 支持、要 copy 模式避免 symlink 问题的用户。

### kamusis/axon-cli

- **symlink hub-and-spoke**：明确以 symlink 为核心
- **20 个 directory targets + file targets**：支持 CLAUDE.md/AGENTS.md/GEMINI.md 多目标 fanout
- **AI security audit**：扫描 skill 内容安全
- **keyword + semantic search**
- **per-skill 或 whole hub rollback**
- **Go binary**：单文件分发，启动快

适合：重度 symlink、要 AI 安全审计、要语义搜索、要文件级 fanout 的用户。

### skillmesh

- **任意 sync 后端**：iCloud / Dropbox / GDrive / Syncthing / 坚果云 / 百度 / 夸克 / 阿里云盘 / git / manual
- **真 peer-write 并发**：双机同时改不靠 git merge，event log + Lamport + CAS
- **完整 lifecycle 状态机**：detach/attach/uninstall/forget/purge/gc 全部可恢复
- **Genesis snapshot + deterministic replay**：可重建任何状态
- **零第三方依赖**：仅 Python3 std-lib
- **单脚本可审计**：直接改 .py，无 build toolchain
- **国产网盘原生支持**：国内市场无对口工具

适合：用 iCloud/国产网盘不走 git、重视 lifecycle 可恢复性、要零依赖可审计源码的用户。

## 选型决策树

```
你要 GUI 还是 CLI？
├── GUI → qufei1993/skills-hub
└── CLI
    ├── 你用 Windows？
    │   ├── 是 → skillstash 或 qufei1993
    │   └── 否
    │       ├── 你需要 AI 安全审计 / 语义搜索？
    │       │   ├── 是 → axon-cli
    │       │   └── 否
    │       │       ├── 你用 iCloud / 国产网盘 不走 git？
    │       │       │   ├── 是 → skillmesh
    │       │       │   └── 否（习惯 git） → skillstash 或 axon-cli
    │       │       └── 你重视 lifecycle 完整可恢复性？
    │       │           ├── 是 → skillmesh
    │       │           └── 否 → 三个都可，看口味
```

## 不与竞品重叠的部分

skillmesh 的核心差异化（其他竞品均无）：

1. **非 git sync 后端**：iCloud / Dropbox / 国产网盘用户
2. **真 peer-write 并发**：双机同时改不冲突
3. **完整 lifecycle 状态机**：所有操作可恢复
4. **零依赖 + 单脚本可审计**

如果你的需求不在这四点，其他竞品可能更适合。skillmesh 不刻意替代任何竞品。

## 迁移到 skillmesh

v1.1+ 将提供 `skillmesh import` 命令从 skillstash / axon-cli / git 导入。当前 v1 需手动迁移：

1. 把竞品 hub 目录下的 skill 子目录复制到 skillmesh watch.dirs
2. `skillmesh scan` 自动识别并入库
3. 验证 status 一致后，可删除竞品工具
