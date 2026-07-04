# Linux 部署指南

Skillmesh 在 Linux 上通过 systemd user unit 跑 daemon，软链接与 macOS 一致。

## 前置条件

- Python 3.9+（多数发行版自带；TOML config 需 3.11+）
- systemd（现代发行版默认）
- `--user` 实例启用（`loginctl enable-linger $USER`）

## 1. 安装

```bash
git clone https://github.com/<you>/skillmesh-cli.git
cd skillmesh-cli
```

无需 pip install，直接 `python3 skillmesh.py` 运行。

## 2. 选择同步后端

Linux 无 iCloud，推荐：

| 后端 | 优势 | 安装 |
| --- | --- | --- |
| **Syncthing** | 开源、P2P、无中心服务器 | `apt install syncthing` / [syncthing.net](https://syncthing.net) |
| **Dropbox** | 商业方案稳定 | [dropbox.com](https://dropbox.com) |
| **Google Drive** | 商业方案 | [google.com/drive](https://google.com/drive) |
| **rclone mount** | 支持数十种云盘 | [rclone.org](https://rclone.org) |
| **git** | 开发者熟悉 | 配置 `sync_backend = "git"`，hub 目录 `git init` |
| **manual** | 不自动同步，手动 rsync | `sync_backend = "manual"` |

### Syncthing 配置示例

1. 安装并启动 syncthing
2. 在 Web UI 添加一个共享文件夹，本地路径如 `~/Sync/`
3. 在另一台机器上同样配置并接受共享

skillmesh config：

```toml
[hub]
path = "~/Sync/skillmesh"
sync_backend = "syncthing"
```

### git 后端配置示例

```bash
mkdir ~/skillmesh-hub && cd ~/skillmesh-hub
git init
git remote add origin git@github.com:you/skillmesh-hub.git
```

skillmesh config：

```toml
[hub]
path = "~/skillmesh-hub"
sync_backend = "git"
```

注意：用 git 后端时，hub 内的 `manifest.json` / `snapshot.json` / `events/` / `blobs/` / `skills/` / `.uninstalled/` 都需 commit（见 `.gitignore` 策略 - runtime hub gitignore 只排除 log/lock）。

同步流程：

```bash
# A 机改动后
cd ~/skillmesh-hub && git add -A && git commit -m "update" && git push

# B 机拉取
cd ~/skillmesh-hub && git pull
skillmesh scan
```

## 3. 初始化 skillmesh

```bash
python3 skillmesh.py init
```

生成 `~/.config/skillmesh/config.toml` + `host.json`。

## 4. 编辑 config

```bash
$EDITOR ~/.config/skillmesh/config.toml
```

```toml
[hub]
path = "~/Sync/skillmesh"
sync_backend = "syncthing"

[[sources]]
label = "work"
prefix = "~/projects/work"

[[agents]]
name = "codex"
dir = "~/.codex/skills"
accept_sources = ["work"]
layout = "directory"

[[formats]]
name = "skill-md"
filename = "SKILL.md"

[watch]
dirs = ["~/projects/work"]
exclude = ["skillmesh", "/.git/", "node_modules"]
```

## 5. 扫描并分发

```bash
python3 skillmesh.py scan
python3 skillmesh.py status
```

## 6. 安装 daemon

```bash
# 启用 --user 实例（一次性）
loginctl enable-linger $USER

# 安装 skillmesh daemon
python3 skillmesh.py install_daemon
```

生成：
- `~/.config/systemd/user/skillmesh.service`（oneshot，执行 scan）
- `~/.config/systemd/user/skillmesh.timer`（每 60 秒触发 service）

查看状态：

```bash
systemctl --user status skillmesh.timer
systemctl --user list-timers | grep skillmesh
```

查看日志：

```bash
tail -f ~/.local/state/skillmesh/logs/daemon.log
```

卸载：

```bash
python3 skillmesh.py uninstall_daemon
```

## 7. 多机部署

每台 Linux 机器：

1. 安装同一同步后端（Syncthing / Dropbox / rclone 等）
2. 配置同步目录指向同一云路径
3. clone skillmesh-cli
4. `python3 skillmesh.py init`（生成新 host UUID）
5. 编辑 config 指向同步路径
6. `python3 skillmesh.py scan`
7. （可选）`install_daemon`

## 跨平台（macOS + Linux 混用）

skillmesh 在 macOS 和 Linux 上行为一致：

- hub 目录结构相同
- event 格式相同
- 软链接语义相同（POSIX symlink）

混用场景：Mac 用 iCloud 同步，Linux 用 rclone mount iCloud Drive。两机 hub 路径不同，但内容一致。

config 各自配置（`hub.path` 不同），但 agents/sources/watch 应一致。

## 故障排查

### systemd --user 服务不启动

确认 linger 启用：

```bash
loginctl show-user $USER | grep Linger
# 应为 Linger=yes
```

如否：

```bash
sudo loginctl enable-linger $USER
```

### 软链接权限问题

Linux 上创建软链接无特殊权限要求。若失败检查：

- 目标目录可写
- 没有同名文件阻塞

### 大文件同步慢

Syncthing / rclone 大文件同步可能慢。skillmesh 不阻塞，下次 scan 自动补齐。status 显示 `SYNC-PENDING`。

## 备份位置

Linux 默认 `~/.local/state/skillmesh/backups/`，符合 XDG 规范。

```bash
python3 skillmesh.py backup
ls ~/.local/state/skillmesh/backups/
```
