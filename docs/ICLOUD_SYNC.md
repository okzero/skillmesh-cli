# iCloud 多机部署指南

Skillmesh 不绑定 iCloud，但 iCloud 是 macOS 用户最常用的同步后端。本文档说明如何把 hub 目录放在 iCloud Drive 下实现多机同步。

## 前置条件

- 2+ 台 Mac，均登录同一 Apple ID
- 均开启 iCloud Drive（系统设置 → Apple ID → iCloud → iCloud Drive）
- Python 3.9+（macOS 自带；TOML config 需 3.11+）

## 1. 在第一台 Mac 上初始化

```bash
git clone https://github.com/<you>/skillmesh-cli.git
cd skillmesh-cli

# 初始化（生成 ~/.config/skillmesh/config.toml + host.json）
python3 skillmesh.py init
```

## 2. 编辑 config 指向 iCloud 路径

```bash
$EDITOR ~/.config/skillmesh/config.toml
```

把 `hub.path` 改为 iCloud Drive 路径：

```toml
[hub]
path = "~/Library/Mobile Documents/com~apple~CloudDocs/skillmesh"
sync_backend = "icloud"
```

iCloud Drive 的本地路径是 `~/Library/Mobile Documents/com~apple~CloudDocs/`。

## 3. 添加 skill 源目录

在 config 里声明 watch.dirs，告诉 skillmesh 从哪些目录扫描 skill。例如：

```toml
[[sources]]
label = "work"
prefix = "~/Documents/work"

[watch]
dirs = ["~/Documents/work", "~/.codex/skills", "~/.claude/skills"]
exclude = ["skillmesh", "/.git/", "node_modules"]
```

## 4. 添加 agent

```toml
[[agents]]
name = "codex"
dir = "~/.codex/skills"
accept_sources = ["work"]
layout = "directory"

[[agents]]
name = "claude-code"
dir = "~/.claude/skills"
accept_sources = ["work"]
layout = "directory"
```

## 5. 扫描并分发

```bash
python3 skillmesh.py scan
python3 skillmesh.py status
```

`status` 应显示所有发现的 skill 已分发到 codex / claude-code 目录（软链指向 hub/skills/）。

## 6. 安装 daemon 自动同步

```bash
python3 skillmesh.py install_daemon
```

会生成 `~/Library/LaunchAgents/com.skillmesh.watch.plist`，每 60 秒自动 scan 一次。

查看日志：

```bash
tail -f ~/Library/Logs/skillmesh/daemon.log
```

卸载：

```bash
python3 skillmesh.py uninstall_daemon
```

## 7. 在第二台 Mac 上接入

1. 等 iCloud 把 hub 目录同步过来（`~/Library/Mobile Documents/com~apple~CloudDocs/skillmesh/` 出现）
2. clone skillmesh-cli 仓库
3. 初始化：

   ```bash
   python3 skillmesh.py init
   ```

   会生成新的 host UUID（与第一台不同）。

4. 编辑 config：与第一台一致（hub.path 同样指向 iCloud 路径，agents 配置相同）。
5. 扫描：

   ```bash
   python3 skillmesh.py scan
   ```

   这次 scan 会重放第一台机器写的事件，把所有 skill 物化到本机 hub/skills/，并建软链到本机 agent 目录。

## iCloud 特定行为

### 占位文件 `.icloud`

iCloud 未下载某文件时，会显示 `<filename>.icloud` 占位文件。skillmesh 自动识别：

- `config.placeholders.suffixes` 默认含 `.icloud`
- 发现占位文件时，对应 skill 标 `SYNC-PENDING`，不误判为 orphan
- 文件下载完成后下次 scan 自动恢复

强制下载某 skill：

```bash
# macOS 触发下载
brctl download ~/Library/Mobile\ Documents/com~apple~CloudDocs/skillmesh/blobs/<hash>
```

### 同步延迟

iCloud 同步不实时（秒级到分钟级）。多机操作后稍等片刻再 scan。status 命令会显示 `SYNC-PENDING` 提示。

### 冲突文件

若两台机器同时改同一文件，iCloud 会生成 `foo (1).ext` 冲突文件。skillmesh 默认识别 `(1)` 模式并跳过，提示用户手动合并。

可在 config 覆盖：

```toml
[conflicts]
patterns = ["\\(\\d+\\)\\.", "冲突", "_conflict"]
```

## 备份

备份不存 hub 内（避免被 iCloud 同步到所有机器，浪费空间）：

```bash
python3 skillmesh.py backup
```

默认存到 `~/Library/Application Support/skillmesh/backups/`。可在 config 改：

```toml
[backup]
path = "~/backups/skillmesh"
```

回滚：

```bash
python3 skillmesh.py rollback            # 用最新备份
python3 skillmesh.py rollback <backup-dir>  # 指定备份
```

## 故障排查

见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。
