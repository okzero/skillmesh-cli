# Skillmesh

> 多 Agent × 多机器 Skill 对等同步工具。一次安装，按平台使用 symlink / junction / managed copy 分发到所有 Agent；hub 目录经任意同步后端跨机同步；所有节点对等，无中心写者。

[English](README_EN.md) | 中文

## 为什么需要 Skillmesh

AI 编码助手（Agent）生态爆发，一名开发者常同时用 2-5 个 Agent：

- codex：`~/.codex/skills/` + `SKILL.md`
- claude code：`~/.claude/skills/` + `SKILL.md` / `CLAUDE.md`
- cursor：`.cursor/rules/` + `.cursorrules`
- windsurf / cline / aider / roo / continue...

每个 Agent 的 skill 存储位置和文件名约定各不相同。Skill 写好后希望被多个 Agent 共享，手动 cp 到每个 Agent 目录 → N 份副本、内容漂移、多机同步困难。

**Skillmesh 解决**：

- **单机多 Agent**：装一次，按平台安全分发到所有声明接收它的 Agent 目录
- **多机同步**：hub 目录放在 iCloud / Dropbox / 国产网盘任意同步路径下，自动收敛
- **节点对等**：任意机器均可读可写，无 master/slave，无 writer/non-writer 区分

## 核心特性

| 特性 | 说明 |
| --- | --- |
| **任意 sync 后端** | iCloud / Dropbox / GDrive / Syncthing / 坚果云 / 百度网盘 / 夸克网盘 / 阿里云盘 / git / manual |
| **Peer-to-peer 多机并发写** | event log 双写 + Lamport 时钟 + CAS blob，双机同时改不冲突 |
| **完整 lifecycle 状态机** | detach / attach / uninstall / forget / purge / gc，全部可恢复 |
| **Config-driven 多 Agent 通用** | 任意 Agent，config 声明，源码零硬编码 |
| **多 skill 格式** | SKILL.md / CLAUDE.md / .cursorrules / .windsurfrules / skill.json / 自定义 |
| **directory / file layout** | macOS/Linux 使用软链；Windows 使用目录 junction，文件软链权限不足时使用受管副本 |
| **跨平台** | macOS（launchd）+ Linux（systemd）+ Windows（Task Scheduler） |
| **零第三方依赖** | 仅 Python3 标准库，`python3 skillmesh.py` 直接运行 |

## 与竞品对比

| 能力 | qufei1993/skills-hub ★1101 | Duducoco/skillstash ★5 | kamusis/axon-cli ★3 | **skillmesh** |
| --- | :-: | :-: | :-: | :-: |
| 形态 | GUI（Tauri） | CLI（npm） | CLI（Go binary） | CLI（Python std-lib） |
| Windows 支持 | ✅ | ✅ | ✅ | ✅ |
| Agent 数量 | 46 内置 | auto-detect + 自定义 | 20 内置 + 自定义 | config 声明（任意） |
| 分发方式 | symlink 优先 + copy fallback | copy only | symlink | symlink / junction / managed copy |
| 多机 sync | Git URL import（非实时） | git remote + 三路合并 | git sync | **任意 sync 后端** |
| 非 git sync 后端（含国产网盘） | ❌ | ❌ | ❌ | ✅ |
| Peer-write 并发（双机同时改） | ❌ | ⚠️（靠 git merge） | ⚠️（靠 git merge） | ✅ |
| CAS blob 存储 | ❌ | ❌ | ❌ | ✅ |
| 完整 lifecycle 状态机 | ❌ | ❌ | ❌ | ✅ |
| 源码可审计（无 build toolchain） | ❌（cargo） | ❌（tsc/npm） | ❌（go build） | ✅ |

各有特点，不刻意对标。详细对比见 `docs/COMPETITORS.md`。

## 快速开始

### 1. 安装

无需安装，直接 clone：

```bash
git clone https://github.com/<you>/skillmesh-cli.git
cd skillmesh-cli
```

要求：Python 3.9+。TOML config 需 3.11+，3.9/3.10 用 JSON config。以下示例在 macOS/Linux 使用 `python3`；Windows 使用 `py -3` 或 `python`。

### 2. 初始化

```bash
python3 skillmesh.py init
```

会生成：
- `~/.config/skillmesh/config.toml`（或 `.json`，按 Python 版本）
- `~/.config/skillmesh/host.json`（host UUID）

Windows 配置存放在 `%APPDATA%\skillmesh`，本机状态、日志与备份存放在 `%LOCALAPPDATA%\skillmesh`。详见 [Windows 部署](docs/WINDOWS_GUIDE.md)。

### 3. 编辑 config

```bash
$EDITOR ~/.config/skillmesh/config.toml
```

关键配置：

```toml
[hub]
path = "~/Library/Mobile Documents/com~apple~CloudDocs/skillmesh"  # 放 iCloud 同步目录下
sync_backend = "icloud"

[[agents]]
name = "codex"
dir = "~/.codex/skills"
accept_sources = ["work", "personal"]
layout = "directory"

[[agents]]
name = "cursor"
dir = "~/.cursor/rules"
accept_sources = ["personal"]
layout = "file"
target_filename = "{skill}.mdc"
```

完整示例见 `config.example.toml` / `config.example.json`。

### 4. 扫描并分发

```bash
python3 skillmesh.py scan
python3 skillmesh.py status
```

### 5. 多机部署

第二台机器：
1. 等 sync 后端把 hub 目录同步过来
2. `python3 skillmesh.py init`（生成新 host UUID）
3. `python3 skillmesh.py scan`（重放 events，建软链）

### 6. 安装 daemon（自动周期扫描）

```bash
python3 skillmesh.py install_daemon
```

macOS 用 launchd，Linux 用 systemd user unit，Windows 用 Task Scheduler。

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `init` | 生成 config 模板 + host UUID + hub 骨架 |
| `scan` | discover → plan → validate → execute + apply |
| `apply` | 仅重建本机分发目标（不改 hub 内容） |
| `adopt` | 首次接入既有 skill 集 |
| `status` | 显示当前状态 |
| `invariants` | 检查不变量 |
| `detach` / `attach` | 暂停 / 恢复某 skill 的分发 |
| `uninstall` / `forget` | 卸载（可恢复）/ 恢复 |
| `purge --yes` | 彻底删除（需确认） |
| `gc` | 清理无引用 blob |
| `compact` | 把 events fold 进 snapshot |
| `backup` / `rollback` | 备份 / 回滚 |
| `install_daemon` / `uninstall_daemon` | 安装 / 卸载 daemon |

所有写命令支持 `--dry-run`；`purge` 需 `--yes`。

## 文档

- [架构设计](docs/ARCHITECTURE.md) - event log + CAS + replay 详细设计
- [iCloud 部署](docs/ICLOUD_SYNC.md) - iCloud 多机部署指南
- [国产网盘部署](docs/CN_CLOUD_DRIVES.md) - 坚果云/百度/夸克/阿里云盘
- [Linux 部署](docs/LINUX_GUIDE.md) - systemd 配置
- [Windows 部署](docs/WINDOWS_GUIDE.md) - junction/copy 与 Task Scheduler
- [竞品对比](docs/COMPETITORS.md) - 与 qufei1993/skillstash/axon-cli 详细对比
- [故障排查](docs/TROUBLESHOOTING.md) - 常见问题

## 设计原则

1. **零硬编码**：路径、用户名、Agent 名全 config 驱动
2. **节点对等**：每机只写自己 `events/<event_dir>/`，无中心写者
3. **真相源单一**：snapshot + events + blobs 跨机同步；manifest 派生可重建
4. **fail-closed**：snapshot/event 损坏不静默覆盖，要求人工介入
5. **零依赖**：仅 Python3 标准库
6. **可审计**：纯 Python 源码可直接读改，无 build toolchain

## License

MIT
