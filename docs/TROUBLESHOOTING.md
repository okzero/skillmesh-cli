# 故障排查

常见问题与解决方法。

## 安装与初始化

### `skillmesh init` 报错 "config already exists"

config 已存在，不会覆盖。如要重置：

```bash
mv ~/.config/skillmesh/config.toml ~/.config/skillmesh/config.toml.bak
python3 skillmesh.py init
```

### `skillmesh init` 生成了 .json 而非 .toml

Python 版本低于 3.11。TOML 需 3.11+ `tomllib`。两个选择：

1. 升级 Python 到 3.11+
2. 用 JSON config（功能等价，只是不能写注释）

### `skillmesh scan` 报 "no config found"

config 文件不在以下任一路径：

- `--config <path>` 参数
- `$SKILLMESH_CONFIG` 环境变量
- `~/.config/skillmesh/config.toml`
- `~/.config/skillmesh/config.json`

运行 `skillmesh init` 生成。

### `skillmesh scan` 报 "TOML config requires Python 3.11+"

config 是 .toml 但 Python < 3.11。两种解决：

```bash
# 方案 1：用 JSON
mv ~/.config/skillmesh/config.toml ~/.config/skillmesh/config.json
# （JSON 不支持注释，需手动删除 // 注释）

# 方案 2：升级 Python
brew install python@3.12
```

## host.json 相关

### 报错 "host.json corrupt"

`~/.config/skillmesh/host.json` 损坏。**不要直接删除**——删除会生成新 UUID，与已有 events/<event_dir>/ 不匹配，导致历史事件丢失。

解决：

1. 备份当前损坏文件：

   ```bash
   cp ~/.config/skillmesh/host.json ~/.config/skillmesh/host.json.corrupt
   ```

2. 查看 hub/events/ 下已有的 event_dir 名（hostname-uuid8 格式），从中恢复 host_id：

   ```bash
   ls ~/Library/Mobile\ Documents/com~apple~CloudDocs/skillmesh/events/
   # 输出如：mac-a-abcd1234
   ```

3. 手动重建 host.json：

   ```json
   {
     "host_id": "abcd1234-...",  // 从 event_dir 反推完整 UUID
     "host_display_name": "mac-a",
     "seq": 0,
     "lamport": 0,
     "created_at": 1720096800123456789
   }
   ```

   或临时用环境变量：

   ```bash
   export SKILLMESH_HOST_ID="abcd1234-..."
   skillmesh scan
   ```

### hostname 变了

hostname 变更不影响 host_id（UUID）。新 event 会写入新 event_dir（新 hostname 前缀），旧 event 仍可读。无需操作。

## Scan 与状态

### `skillmesh status` 显示 ORPHAN

skill 在 manifest 中但 `hub/skills/<name>/` 不存在。可能原因：

1. blob 未同步完成（其他机器写了 event 但 blob 还没 sync 过来）
2. 误删了 hub/skills/<name>/

解决：

```bash
# 检查 blob 是否存在
ls <hub>/blobs/

# 若 blob 存在，重新物化
python3 skillmesh.py scan  # 自动 materialize_missing
```

### `skillmesh status` 显示 WRONG-TARGET

软链存在但指向错误。下次 scan 自动修复：

```bash
python3 skillmesh.py scan
```

或强制重建所有软链：

```bash
python3 skillmesh.py apply
```

### `skillmesh status` 显示 SYNC-PENDING

sync 后端未完成下载（占位文件存在）。等同步完成后再次 scan。

强制触发 iCloud 下载：

```bash
brctl download <path-to-file>
```

### `skillmesh scan` 没发现新 skill

检查：

1. config 的 `watch.dirs` 是否包含 skill 所在目录
2. skill 目录是否包含 config `formats` 中声明的文件（如 `SKILL.md`）
3. 目录名是否符合 `^[a-zA-Z0-9._-]+$`（无空格、斜杠等）
4. 目录是否在 `watch.exclude` 中

调试：

```bash
python3 -c "
from skillmesh.config import load_config
from skillmesh.discover import discover
c = load_config()
r = discover(c)
print('candidates:', [x.name for x in r.candidates])
print('skipped:', [x.name for x in r.skipped])
print('warnings:', r.warnings)
"
```

## Lifecycle

### `skillmesh purge` 报 "ConfirmationRequired"

purge 需 `--yes` 显式确认：

```bash
python3 skillmesh.py purge my-skill --yes
```

### `skillmesh uninstall` 报 "PathEscape"

skill 名包含非法字符或路径攻击嫌疑。检查 skill 名仅含 `[a-zA-Z0-9._-]`。

### `skillmesh forget` 报 "skill not in .uninstalled/"

该 skill 未被 uninstall。forget 只能恢复已 uninstall 的 skill。

## 多机同步

### 第二台机器 scan 后没看到第一台的 skill

检查：

1. sync 后端是否已把 hub 目录同步过来

   ```bash
   ls <hub>/events/  # 应有第一台的 event_dir
   ```

2. config 的 `hub.path` 两机是否指向同一同步路径
3. host.json 是否生成（`skillmesh init`）

### 双机同时改同一 skill，结果不对

skillmesh 按 F6.4 冲突规则裁决：

1. 双方都有 SemVer：高者胜
2. 双方都无 SemVer：Lamport 高者胜
3. 一方有 SemVer 一方无：标 `MIXED-VERSION-CONFLICT`，不自动覆盖

查看冲突：

```bash
python3 skillmesh.py status
# 应显示 MIXED-VERSION-CONFLICT
```

两 blob 都保留，可手动选择保留哪个版本（v1.1+ 提供 `skillmesh resolve` 命令）。

### event 文件损坏

event 是跨机真相源。schema、JSON 或文件名 checksum 任一校验失败时，
skillmesh 会停止整个 replay，不移动文件，也不会用部分 event 集合覆盖
manifest。先等待同步客户端完成下载，再重试：

```bash
python3 skillmesh.py scan
```

若仍失败，从另一台健康机器或 backup 恢复报错中指明的 event。不要直接删除；
删除 append-only event 可能让所有机器收敛到错误状态。

## Daemon

### macOS：daemon 不运行

```bash
launchctl list | grep skillmesh
# 应看到 com.skillmesh.watch
```

如无：

```bash
python3 skillmesh.py install_daemon
```

查看日志：

```bash
tail -50 ~/Library/Logs/skillmesh/daemon.log
tail -50 ~/Library/Logs/skillmesh/daemon.err
```

### Linux：systemd timer 不触发

```bash
systemctl --user status skillmesh.timer
systemctl --user list-timers | grep skillmesh
```

如未启用：

```bash
loginctl enable-linger $USER  # 一次性
python3 skillmesh.py install_daemon
```

### daemon 报 "another skillmesh daemon is running"

flock 互斥锁阻止多实例。检查是否有僵尸进程：

```bash
ps aux | grep skillmesh
# 如有残留进程
kill <pid>
```

或删除锁文件：

```bash
rm ~/.local/state/skillmesh/daemon.lock  # macOS: ~/Library/Application Support/skillmesh/daemon.lock
```

## 备份与回滚

### `skillmesh backup` 报错 "permission denied"

备份目录不可写。检查 config 的 `[backup].path`：

```bash
$EDITOR ~/.config/skillmesh/config.toml
# [backup]
# path = "~/backups/skillmesh"  # 改为可写路径
```

### `skillmesh rollback` 报 "hash mismatch"

备份文件损坏或被篡改。**拒绝回滚是安全行为**。检查：

```bash
ls <backup-dir>/
cat <backup-dir>/hashes.json
```

如确认备份有效但 hash 计算有 bug，可手动解 tar：

```bash
cd <hub>
tar xzf <backup-dir>/hub.tar
```

但**不推荐**——hash 不匹配通常意味着数据真的损坏。

## 性能

### scan 很慢

可能原因：

1. watch.dirs 包含大目录（如 `~/Documents` 全量）
2. blob 数量巨大
3. events 数量巨大（未 compact）

解决：

1. 收窄 watch.dirs 到具体子目录
2. 定期 compact：

   ```bash
   python3 skillmesh.py compact
   ```

   把 events fold 进 snapshot，减少重放开销。

3. 检查 exclude 是否漏配（如 `node_modules` / `.venv`）

## 数据损坏恢复

### snapshot.json 损坏

skillmesh **fail-closed**，不会静默覆盖。报错信息会提示恢复：

```bash
skillmesh rollback  # 用最新备份还原
```

如无备份，且 events 完整，可删除 snapshot 重建（丢失 included_events 折叠信息，需重放所有 events）：

```bash
mv <hub>/snapshot.json <hub>/snapshot.json.corrupt
# 编辑删除后的 snapshot，重置为空：
echo '{"version":1,"skills":{},"tombstones":{},"included_events":[]}' > <hub>/snapshot.json
skillmesh scan  # 重放所有 events
```

### manifest.json 损坏

自动重建，无需操作。skillmesh scan 会重命名为 `.corrupt` 并从 snapshot + events 重建。

### blobs 目录损坏

blob 是 CAS（content-addressed），只要源 skill 存在，可重新入库：

```bash
# 把 hub/skills/ 下的 skill 复制回 watch.dirs
# 然后 scan 重新入库
```

或从备份还原：

```bash
skillmesh rollback
```

## 仍无法解决

提交 issue：

- 附 `skillmesh status` 输出
- 附 `skillmesh invariants` 输出
- 附 daemon 日志（`~/Library/Logs/skillmesh/daemon.log` 或 `~/.local/state/skillmesh/logs/daemon.log`）
- 附 config（移除敏感路径）
- 说明 OS / Python 版本 / sync 后端
