# Skillmesh 架构设计

本文档是 `skillmesh` 内部技术设计，配合 `docs/PRD.md` 使用。PRD 定义"做什么"，本文档定义"怎么做"。

## 1. 总体架构

### 1.1 分层

```
┌──────────────────────────────────────────────────────────┐
│  CLI 层（skillmesh.py + skillmesh/cli.py）                │
│  argparse dispatch + 命令默认行为表（PRD §8.4）           │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│  管线层（pipeline.py）                                    │
│  discover → plan → validate → execute 四阶段              │
└──────┬──────────┬──────────┬──────────┬──────────────────┘
       │          │          │          │
┌──────▼──┐ ┌────▼───┐ ┌────▼───┐ ┌────▼──────────────────┐
│discover │ │ config │ │ host   │ │ lifecycle / backup    │
│         │ │        │ │        │ │ platform_daemon       │
└─────────┘ └────────┘ └────────┘ └───────────────────────┘
       │
┌──────▼───────────────────────────────────────────────────┐
│  真相源层（跨机 sync）                                    │
│  ┌────────────┐  ┌──────────┐  ┌─────────┐  ┌─────────┐  │
│  │ snapshot   │  │ events/  │  │ blobs/  │  │.uninst/ │  │
│  │ .json      │  │<event_dir>│  │<hash>/  │  │         │  │
│  └────────────┘  └──────────┘  └─────────┘  └─────────┘  │
└──────────────────────────────────────────────────────────┘
       │
┌──────▼───────────────────────────────────────────────────┐
│  派生层（本机）                                           │
│  manifest.json（重放缓存）+ skills/<name>/（blob 物化）   │
│  + 软链到 Agent 目录                                       │
└──────────────────────────────────────────────────────────┘
```

### 1.2 关键不变量

1. **跨机真相源** = `snapshot.json` + `events/<event_dir>/*` + `blobs/<content_hash>/*` + `.uninstalled/*`
2. **本机派生** = `manifest.json` + `skills/<name>/`（可随时删除重建）
3. **每机只写自己** `events/<event_dir>/` 子目录，hostname 进路径仅作可读前缀，uuid8 决定唯一性
4. **manifest 仅 `event_fingerprint` 变化时写**（保证空 scan 幂等）

---

## 2. 数据模型

### 2.1 Skill Entry（跨机真相源中的 skill 表示）

```json
{
  "name": "my-skill",
  "source": "work",
  "in_hub": true,
  "format": "skill-md",
  "version": "1.0.0",
  "blob_hash": "sha256:abc...",
  "content_hash": "sha256:def..."
}
```

- `name`：skill 标识，仅字母数字 `.` `-` `_`，最长 64 字符
- `source`：用户自定义 label（跨机 metadata，B 机无需配 `sources[].prefix` 即可接收）
- `in_hub`：是否物化到 `skills/<name>/`
- `format`：识别时命中的 format name（来自 config `[[formats]]`）
- `version`：SemVer 字符串（从 `SKILL.md` frontmatter / `skill.json` version 字段自动提取），无则空串
- `blob_hash`：blob 实体 sha256，校验 blob 完整性
- `content_hash`：skill 目录规范化 merkle hash，决定 CAS 地址 `blobs/<content_hash>/`

### 2.2 content_hash vs blob_hash

| 字段 | 含义 | 计算 | 用途 |
| --- | --- | --- | --- |
| `content_hash` | skill 目录规范化 merkle root | 见 §3.2 | CAS 寻址（`blobs/<content_hash>/`） |
| `blob_hash` | blob 实体 sha256 | 对 `blobs/<content_hash>/` 整目录 tgz 后 sha256 | blob 完整性校验（备份/还原时验） |

单文件 skill：blob 内仅一文件，`blob_hash` 与 `content_hash` 数值可能不同（前者是 tgz 整体，后者是 merkle root），但二者一一对应。目录 skill 同理。

---

## 3. CAS Blob 存储

### 3.1 存储布局

```
blobs/
└── <content_hash>/
    ├── .meta.json          # blob 元信息
    ├── SKILL.md            # 单文件 skill 内容
    └── ...                 # 目录 skill 的所有文件
```

`.meta.json`：

```json
{
  "content_hash": "sha256:def...",
  "blob_hash": "sha256:abc...",
  "skill_name": "my-skill",
  "format": "skill-md",
  "version": "1.0.0",
  "created_at": 1720096800123456789,
  "created_by_host": "abcd1234-..."
}
```

### 3.2 content_hash 计算（规范化 merkle hash）

```
content_hash = sha256(
  "skillmesh-blob-v1\n" +
  "name=" + skill_name + "\n" +
  "format=" + format_name + "\n" +
  "version=" + version + "\n" +
  "files:\n" +
  for each file in sorted(files, key=relative_path):
    relative_path + "\n" +
    "  " + sha256(file_content_in_utf8_normalized) + "\n" +
    "  " + str(file_size) + "\n"
)
```

**规范化规则**：

1. 文件按相对路径字典序排序（POSIX path sort）
2. 文件内容 UTF-8 normalize（NFC），换行符统一为 `\n`（CRLF → LF）
3. 文件权限、mtime、owner **不**进 hash（仅内容）
4. 空文件、空目录均纳入（保证目录结构一致）
5. 隐藏文件（`.hidden`）纳入
6. 排除：`.DS_Store`、`Thumbs.db`、`__pycache__`、`.pyc`、`.skillmesh.lock`

### 3.3 写入流程（原子）

```python
def write_blob(skill_dir, skill_name, format_name, version):
    content_hash = compute_content_hash(skill_dir, skill_name, format_name, version)
    blob_dir = BLOBS_DIR / content_hash
    if blob_dir.exists():
        return content_hash  # CAS 命中，幂等
    tmp_dir = BLOBS_DIR / (".tmp." + uuid.uuid4().hex)
    try:
        copy_tree_normalized(skill_dir, tmp_dir)
        write_meta(tmp_dir, content_hash, ...)
        blob_hash = compute_blob_hash(tmp_dir)
        update_meta(tmp_dir, blob_hash)
        os.rename(tmp_dir, blob_dir)  # 原子
        return content_hash
    except:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
```

### 3.4 物化（blob → skills/<name>/）

`skills/<name>/` 是 working view，从 blob 还原：

```python
def materialize(content_hash, skill_name):
    blob_dir = BLOBS_DIR / content_hash
    target = SKILLS_DIR / skill_name
    if target.is_symlink() or target.exists():
        safe_remove(target)  # 仅删 symlink 或空目录
    copy_tree(blob_dir, target, exclude_meta=True)
```

---

## 4. Event Log

### 4.1 文件命名

```
events/<event_dir>/<lamport>-<seq>-<checksum>.json
```

- `event_dir = <portable-host-prefix>-<uuid8>`（如 `mac-a-abcd1234`）；原始 hostname 只展示，路径前缀会替换 Windows 非法字符并避免 `.` 开头
- 文件名 = `<lamport>-<seq>-<checksum>`，三者确定文件唯一性
- `checksum` = sha256(event content 前 16 字符，防冲突文件名碰撞

### 4.2 Event Schema

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "host": "abcd1234-5678-90ef-1234-567890abcdef",
  "host_display_name": "mac-a.local",
  "ts": 1720096800123456789,
  "seq": 1,
  "lamport": 1,
  "op": "add",
  "skill": {
    "name": "my-skill",
    "source": "work",
    "in_hub": true,
    "format": "skill-md",
    "version": "1.0.0",
    "blob_hash": "sha256:abc...",
    "content_hash": "sha256:def..."
  },
  "prev_lamport": 0,
  "schema_version": 1
}
```

- `id`：UUID v4，全局唯一
- `host`：host_id (UUID)，用于确定性排序
- `host_display_name`：hostname，仅展示
- `ts`：纳秒时间戳，仅展示与调试
- `seq`：本 host 内单调递增整数，持久化于 `~/.config/skillmesh/host.json`
- `lamport`：Lamport 时钟值，见 §6
- `op`：`add` / `update` / `detach` / `attach` / `uninstall` / `forget` / `purge` / `gc_prepare` / `gc`
- `skill`：完整 skill entry
- `prev_lamport`：本 host 上一个 event 的 lamport，便于 debug 因果链
- `schema_version`：event schema 版本，未来兼容性

### 4.3 写入协议（原子）

```python
def write_event(op, skill_entry):
    with flock(SEQ_FILE):  # 跨进程互斥
        seq = read_seq() + 1
        lamport = max(local_lamport, last_written_lamport) + 1
        event = build_event(op, skill_entry, seq, lamport)
        content = json.dumps(event, sort_keys=True, ensure_ascii=False)
        checksum = sha256(content)[:16]
        filename = f"{lamport}-{seq}-{checksum}.json"
        tmp = EVENT_DIR / (".tmp." + filename)
        tmp.write_text(content)
        os.rename(tmp, EVENT_DIR / filename)  # 原子
        write_seq(seq)
        local_lamport = lamport
    return event
```

### 4.4 Schema 校验

写入前必校验：

```python
EVENT_SCHEMA = {
    "id": str,           # UUID v4
    "host": str,         # UUID v4
    "host_display_name": str,
    "ts": int,           # nanoseconds
    "seq": int,          # > 0
    "lamport": int,      # > 0
    "op": str,           # enum
    "skill": dict,       # skill entry
    "prev_lamport": int,
    "schema_version": int,
}
```

校验失败 → fail-closed，不写不重放。

---

## 5. Genesis Snapshot

### 5.1 结构

```json
{
  "version": 1,
  "schema_version": 1,
  "created_by_host": "abcd1234-...",
  "created_at": 1720096800123456789,
  "skills": {
    "my-skill": { ... skill entry ... }
  },
  "included_events": ["550e8400-...", "550e8401-..."],
  "content_hash": "sha256:..."
}
```

- `included_events`：已被 fold 进此 snapshot 的 event id 列表，重放时跳过
- `content_hash`：snapshot 自身 hash（防篡改），按 §3.2 规范对 `skills` + `included_events` 计算

### 5.2 生成时机

| 时机 | 触发 | 内容 |
| --- | --- | --- |
| genesis | `skillmesh init` | `skills={}`，`included_events=[]` |
| compact | events 数 > `compact_threshold`（默认 1000）；或 `skillmesh compact` 显式触发 | 将当前 events 重放结果 fold 为新 snapshot |

### 5.3 Compact 流程（原子）

```python
def compact():
    # 1. 重放所有 events，得当前 logical state
    state = replay_all_events()
    # 2. 构建新 snapshot
    new_snapshot = {
        "version": 1,
        "created_by_host": HOST_ID,
        "created_at": now_ns(),
        "skills": state.skills,
        "included_events": state.processed_event_ids,
    }
    new_snapshot["content_hash"] = compute_snapshot_hash(new_snapshot)
    # 3. 原子写
    tmp = SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(new_snapshot, sort_keys=True))
    verify_hash(tmp, new_snapshot["content_hash"])
    os.rename(tmp, SNAPSHOT_FILE)
    # 4. 旧 events 不删（保留可追溯；GC 时清理）
```

### 5.4 校验

加载 snapshot 时：

```python
def load_snapshot():
    data = json.loads(SNAPSHOT_FILE.read_text())
    expected = data.pop("content_hash")
    actual = compute_snapshot_hash(data)
    if expected != actual:
        raise FailClosed("snapshot content_hash mismatch")
    return data
```

---

## 6. Lamport Clock

### 6.1 本地维护

```python
class LamportClock:
    def __init__(self):
        self.local = 0  # 本机已知最大 lamport

    def tick(self):
        self.local += 1
        return self.local

    def observe(self, remote_lamport):
        self.local = max(self.local, remote_lamport) + 1
```

`local` 持久化于 `~/.config/skillmesh/host.json` 的 `lamport` 字段，进程退出时保存。

### 6.2 因果序

event A 因果先于 event B 当且仅当 `A.lamport < B.lamport`，或 `A.lamport == B.lamport` 且 `A.host_id < B.host_id`。

Lamport 相同（不同 host）按 `(host_id, seq, id)` 确定性排序，理论上不可能（不同 host 必有不同的 host_id），但作 fallback。

---

## 7. Deterministic Replay

### 7.1 重放算法

```python
def replay(snapshot, event_files):
    state = State(snapshot.skills, snapshot.tombstones)
    # 收集所有 event
    events = []
    for f in event_files:
        e = load_and_validate(f)
        if e["id"] in snapshot["included_events"]:
            continue  # 已 fold
        events.append(e)
    # 确定性排序
    events.sort(key=lambda e: (e["lamport"], e["host"], e["seq"], e["id"]))
    # 检查重复 (host, seq)
    seen = set()
    for e in events:
        key = (e["host"], e["seq"])
        if key in seen:
            raise FailClosed(f"duplicate (host, seq): {key}")
        seen.add(key)
        apply_event(state, e)
    return state
```

### 7.2 Apply 规则

| op | 应用规则 |
| --- | --- |
| `add` | 若 tombstone 标 uninstalled/purging，跳过；否则 upsert skill entry |
| `update` | 同 add，但触发 F6.4 冲突裁决 |
| `detach` | skill.target_override = [] |
| `attach` | skill.target_override = null |
| `uninstall` | tombstone[name] = {state: "uninstalled", ts, host} |
| `forget` | 删 tombstone[name]，恢复 skill 为 active |
| `purge` | tombstone[name] = {state: "purging", ts, host} |
| `gc_prepare` | tombstone[name] = {state: "gc_prepare", ts, host} |
| `gc` | 删 tombstone[name] + 删 skill entry + 删 blob（如无引用） |

### 7.3 冲突裁决（F6.4）

```python
def resolve_conflict(existing, incoming):
    a_ver = parse_semver(existing.version)
    b_ver = parse_semver(incoming.version)
    if a_ver is not None and b_ver is not None:
        return existing if a_ver >= b_ver else incoming  # SemVer 高者胜
    if a_ver is None and b_ver is None:
        return existing if existing.lamport >= incoming.lamport else incoming  # Lamport 高者胜
    # 一方有 SemVer 一方无
    return ConflictMarker("MIXED-VERSION-CONFLICT", existing, incoming)  # 不自动覆盖
```

`MIXED-VERSION-CONFLICT` 时：两 blob 都保留，manifest 标 conflict，status 提示用户 `skillmesh resolve <name> --keep A|B`。

---

## 8. Lifecycle 状态机

### 8.1 状态图

```
                   ┌─────────┐
                   │ active  │◄──────────┐
                   └────┬────┘           │
              detach    │    attach      │
                   ┌────▼────┐           │
                   │detached │───────────┤
                   └────┬────┘           │
              uninstall │                │
                   ┌────▼────┐    forget │
                   │uninstall│──────────►┤
                   │  -ed    │           │
                   └────┬────┘           │
              purge --yes│                │
                   ┌────▼────┐           │
                   │purging  │           │
                   └────┬────┘           │
                  gc     │                │
                   ┌────▼────┐           │
                   │ removed │           │
                   └─────────┘           │
```

### 8.2 中间态与 finalize

`uninstall` / `forget` / `purge` 写 tombstone 后，scan 时 finalize：

```python
def finalize_lifecycle(state):
    for name, tomb in state.tombstones.items():
        if tomb["state"] == "pending":
            # uninstall 中间态：撤链 + 移入 .uninstalled/
            unlink_skill(name)
            move_to_uninstalled(name)
            tomb["state"] = "uninstalled"
        elif tomb["state"] == "restoring":
            # forget 中间态：从 .uninstalled/ 移回 + 分发
            move_from_uninstalled(name)
            apply_skill_links(name)
            del state.tombstones[name]
        elif tomb["state"] == "purging":
            # 等待跨机 sync 完成（双机都看到 purging 后才 gc）
            if all_hosts_seen_purge(name):
                tomb["state"] = "gc_prepare"
```

### 8.3 GC 三阶段

```
gc_prepare:  所有机器标记 ready
       ↓
delete:      删 .uninstalled/<name>/ + 删 skills/<name>/
       ↓
gc:          删 blob（如无其他 skill 引用）+ 删 tombstone
```

跨机同步延迟保护：GC 前检查所有已知 host 是否都写过 `purge` event，未到齐则等待，不误删。

---

## 9. 四阶段管线

### 9.1 Discover

```python
def discover(config):
    candidates = []
    for watch_dir in config.watch.dirs:
        for root, dirs, files in walk(watch_dir, exclude=config.watch.exclude):
            for fmt in config.formats:
                if fmt.filename in files:
                    skill_name = basename(root)
                    if not is_valid_skill_name(skill_name):
                        warn(f"skip invalid name: {skill_name}")
                        continue
                    source = derive_source(root, config.sources)
                    version = extract_version(root, fmt)
                    candidates.append(SkillCandidate(
                        name=skill_name, path=root,
                        source=source, format=fmt.name, version=version))
                    break  # 一个目录命中一个 format 即可
    return dedupe_by_name(candidates)
```

跳过规则：
- 软链（避免扫 hub 自身的 skills/）
- `config.watch.exclude` 命中
- `config.conflicts.patterns` 命中
- `config.placeholders.suffixes` 命中 → 标 SYNC-PENDING
- 已是 manifest 中某 entry 的 `source_path` → skip

### 9.2 Plan

```python
def plan(candidates, manifest):
    ops = []
    for c in candidates:
        existing = manifest.skills.get(c.name)
        if existing is None:
            ops.append(Op("ADD", c))
        elif content_hash(c) != existing.content_hash:
            ops.append(Op("UPDATE", c, existing))
        else:
            ops.append(Op("IGNORE", c))
    # 检查 manifest 中有但 candidates 无的 → ORPHAN
    for name, entry in manifest.skills.items():
        if name not in [c.name for c in candidates]:
            ops.append(Op("ORPHAN", entry))
    # LINK/UNLINK 操作
    for name, entry in manifest.skills.items():
        targets = compute_targets(entry, config.agents)
        current_links = read_current_links(name, config.agents)
        if set(targets) != set(current_links):
            ops.append(Op("RELINK", name, targets, current_links))
    return ops
```

### 9.3 Validate

```python
def validate(ops):
    for op in ops:
        if op.type in ("ADD", "UPDATE"):
            if not op.candidate.path.exists():
                op.block("source missing")
            if is_placeholder(op.candidate.path):
                op.block("SYNC-PENDING")
        if op.type == "RELINK":
            for agent in op.targets:
                if not agent.dir.parent.exists():
                    op.block(f"agent parent missing: {agent.dir}")
        if op.type == "ORPHAN":
            if is_in_uninstalled(op.entry):
                op.skip()  # 已隔离，不算 orphan
    return ops
```

### 9.4 Execute

```python
def execute(ops, dry_run=False):
    for op in ops:
        if op.blocked:
            warn(op)
            continue
        if dry_run:
            print(op)
            continue
        try:
            if op.type == "ADD":
                content_hash = write_blob(op.candidate)
                write_event("add", build_entry(op.candidate, content_hash))
                materialize(content_hash, op.candidate.name)
            elif op.type == "UPDATE":
                # 冲突裁决
                resolve_and_update(op)
            elif op.type == "RELINK":
                relink(op.name, op.targets)
            # ...
        except Exception as e:
            error(f"{op} failed: {e}")
            rollback_op(op)
```

### 9.5 事务化移动

ADD/UPDATE 时：

```
1. copy skill_dir → blob/.tmp.xxx/
2. compute content_hash, blob_hash
3. rename .tmp → blobs/<content_hash>/  (原子)
4. write event (add/update)
5. materialize blob → skills/<name>/
6. apply links to agents
7. (源目录保留，由用户决定删除；migrate 时删源)
```

任一步失败 → 回滚该 skill（删 tmp，不动 blob/event/links）。

---

## 10. Host UUID 机制

### 10.1 host.json

```json
{
  "host_id": "abcd1234-5678-90ef-1234-567890abcdef",
  "host_display_name": "mac-a.local",
  "seq": 42,
  "lamport": 100,
  "created_at": 1720096800123456789
}
```

### 10.2 生成与读取

```python
def load_or_create_host():
    if env := os.environ.get("SKILLMESH_HOST_ID"):
        return Host(host_id=env, display_name=socket.gethostname(), ...)
    if HOST_FILE.exists():
        return Host.from_json(HOST_FILE.read_text())
    # 首次生成
    host = Host(
        host_id=str(uuid.uuid4()),
        display_name=socket.gethostname(),
        seq=0,
        lamport=0,
        created_at=now_ns(),
    )
    tmp = HOST_FILE.with_suffix(".tmp")
    tmp.write_text(host.to_json())
    os.rename(tmp, HOST_FILE)
    return host
```

### 10.3 hostname 变更保护

hostname 仅写入 `host_display_name` 和 `event_dir` 前缀，**不进 event 内 `host` 字段**。hostname 变更后：

- `host_id` 不变 → event 排序不变
- `event_dir` 旧目录名仍可读（hostname 旧值 + uuid8），新 event 写入新 `event_dir`
- replay 时扫描所有 `events/*/`，不依赖目录名匹配 host

---

## 11. 故障恢复

### 11.1 manifest 损坏

```python
def load_manifest():
    try:
        return json.loads(MANIFEST_FILE.read_text())
    except (JSONDecodeError, KeyError):
        warn("manifest corrupt, rebuilding from snapshot + events")
        os.rename(MANIFEST_FILE, MANIFEST_FILE.with_suffix(".corrupt"))
        return rebuild_manifest()  # snapshot + replay
```

### 11.2 snapshot 损坏

```python
def load_snapshot():
    try:
        data = json.loads(SNAPSHOT_FILE.read_text())
        verify_hash(data)
        return data
    except (JSONDecodeError, HashMismatch):
        # fail-closed：不静默覆盖
        raise FailClosed(
            "snapshot corrupt or tampered. Restore from backup: "
            "`skillmesh rollback <backup-file>`"
        )
```

### 11.3 event 文件损坏

```python
def load_events():
    for f in EVENT_DIR.glob("*.json"):
        try:
            event = load_and_validate(f)
            verify_filename_checksum(f, event)
            yield event
        except SchemaError as e:
            # fail-closed：不能用部分 event 集合派生 manifest。
            # 不移动共享真相源；等待同步完成或从 backup 恢复。
            raise FailClosed(f"corrupt event {f.name}: {e}")
```

### 11.4 backup/rollback

```python
def backup():
    backup_dir = Path(config.backup.path) / f"{now_ns()}-{uuid.uuid4().hex[:8]}"
    backup_dir.mkdir(parents=True)
    tar = backup_dir / "hub.tar"
    with tarfile.open(tar, "w:gz") as tf:
        for name in ["snapshot.json", "manifest.json", "events", "blobs",
                     "skills", ".uninstalled"]:
            tf.add(HUB_DIR / name, arcname=name)
    # 写 hash 清单
    (backup_dir / "hashes.json").write_text(compute_hashes(tar))
    return tar

def rollback(tar_path):
    # 1. 校验 hash
    # 2. 在修改 hub 前验证全部 member 路径与类型
    # 3. 删 hub/snapshot/events/blobs/skills/.uninstalled
    # 4. 解 tar 还原
    # 5. scan 重建 manifest + apply 重建本机分发目标
```

---

## 12. 数据流（典型场景）

### 12.1 单机首次安装

```
1. skillmesh init
   → 生成 ~/.config/skillmesh/config.{toml,json}
   → 生成 ~/.config/skillmesh/host.json
   → 创建 hub/ 骨架（blobs/, events/<event_dir>/, skills/, .uninstalled/）
   → 写 genesis snapshot（skills={}, included_events=[]）

2. 用户在 watch.dirs 下放 skill 目录

3. skillmesh scan
   → discover: 找到 skill 候选
   → plan: ADD ops
   → validate: 通过
   → execute:
     - write_blob (CAS)
     - write_event(add)
     - materialize
     - apply links 到 agents

4. skillmesh status
   → 显示已分发
```

### 12.2 第二台机器接入

```
1. B 机 sync_backend 完成 hub 同步
   → snapshot.json, events/, blobs/, skills/, .uninstalled/ 在 B 机可见

2. B 机 skillmesh init
   → 生成 B 机 host.json (不同 host_id)
   → 检测到 hub 已有 snapshot，不覆写

3. B 机 skillmesh scan
   → manifest 重放（snapshot + events）→ B 机 manifest 与 A 一致
   → apply: 在 B 机 agent 目录建软链（指向 B 机 hub/skills/）
```

### 12.3 双机同时新增不同 skill

```
A 机: skillmesh scan → 写 events/<A_event_dir>/add-skillA
B 机: skillmesh scan → 写 events/<B_event_dir>/add-skillB
sync 后两机都有两个 event
A 机下次 scan: replay 两 event → skillA + skillB 都在 manifest → apply 分发
B 机同理
```

### 12.4 双机同时更新同一 skill（无 SemVer）

```
A 机: update my-skill, lamport=100
B 机: update my-skill, lamport=101 (B observe A 后)
sync 后:
- A event (lamport=100) + B event (lamport=101) 都在
- replay 按 lamport 排序，B 后写
- B 胜，A 的 blob 保留可恢复
- status 标 VERSIONLESS-CONFLICT 提示合并
```

### 12.5 双机同时更新（一方有 SemVer 一方无）

```
A: my-skill v1.0.0, lamport=100
B: my-skill (no version), lamport=101
sync 后:
- F6.4 mixed → 不自动覆盖
- 两 blob 保留
- manifest 标 MIXED-VERSION-CONFLICT
- status 提示: skillmesh resolve my-skill --keep A|B
```

---

## 13. 配置加载

### 13.1 路径优先级

```
1. --config <path>          (CLI 参数)
2. $SKILLMESH_CONFIG        (环境变量)
3. ~/.config/skillmesh/config.toml
4. ~/.config/skillmesh/config.json
```

### 13.2 双格式

```python
def load_config(path):
    if path.suffix == ".toml":
        if sys.version_info < (3, 11):
            raise ConfigError(
                "TOML config requires Python 3.11+. "
                "Use config.json instead or upgrade Python."
            )
        import tomllib
        with open(path, "rb") as f:
            return normalize(tomllib.load(f))
    elif path.suffix == ".json":
        return normalize(json.loads(path.read_text()))
    else:
        raise ConfigError(f"unsupported config format: {path.suffix}")

def normalize(raw):
    # 统一字段名（已统一 plural: sources/agents/formats）
    # 校验必填字段
    # 填充默认值
    return Config(...)
```

### 13.3 Schema 校验

```python
REQUIRED = {
    "hub": ["path"],
    "agents": [("name", "dir", "accept_sources", "layout")],
    "watch": ["dirs"],
}
OPTIONAL = {
    "hub": ["sync_backend"],
    "sources": [("label", "prefix")],
    "formats": [("name", "filename")],
    "watch": ["interval", "exclude"],
    "conflicts": ["patterns"],
    "placeholders": ["suffixes"],
    "backup": ["path"],
}
```

校验 `layout=file` 时必填 `target_filename` 且含 `{skill}`。

---

## 14. 跨平台 daemon

### 14.1 macOS launchd

`~/Library/LaunchAgents/com.skillmesh.watch.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.skillmesh.watch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>PATH_TO/skillmesh.py</string>
    <string>scan</string>
  </array>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>PATH_TO/logs/daemon.log</string>
  <key>StandardErrorPath</key>
  <string>PATH_TO/logs/daemon.err</string>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
```

### 14.2 Linux systemd

`~/.config/systemd/user/skillmesh.service`：

```ini
[Unit]
Description=Skillmesh watch daemon

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 PATH_TO/skillmesh.py scan
StandardOutput=append:/home/USER/.local/state/skillmesh/logs/daemon.log
StandardError=append:/home/USER/.local/state/skillmesh/logs/daemon.err
```

`~/.config/systemd/user/skillmesh.timer`：

```ini
[Unit]
Description=Run Skillmesh scan periodically

[Timer]
OnBootSec=1min
OnUnitActiveSec=60s

[Install]
WantedBy=timers.target
```

### 14.3 互斥锁

```python
LOCK_FILE = Path("~/.local/state/skillmesh/daemon.lock").expanduser()

def acquire_daemon_lock():
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise AlreadyRunning("another skillmesh daemon is running")
    return lock_fd
```

---

## 15. 性能考量

| 操作 | 复杂度 | 优化 |
| --- | --- | --- |
| discover | O(watch_dirs 文件数) | 跳过软链、exclude、缓存 mtime |
| content_hash | O(skill 文件数 × 文件大小) | 增量 hash（仅变化的文件） |
| replay | O(events 数) | compact 减少 events 数 |
| apply | O(skills × agents) | 并行建链 |
| status | O(skills × agents) | 缓存软链状态 |

目标：100 skill + 5 agent，scan < 2s（NF1）。

---

## 16. 安全考量

### 16.1 隔离路径攻击

`uninstall`/`forget`/`purge` 操作 `.uninstalled/<name>/`，必须：

```python
def safe_uninstall_path(name):
    if not re.match(r"^[a-zA-Z0-9._-]+$", name):
        raise InvalidName(name)
    target = (UNINSTALLED_DIR / name).resolve()
    if not str(target).startswith(str(UNINSTALLED_DIR.resolve())):
        raise PathEscape(name)
    if target.is_symlink():
        raise SymlinkInUninstalled(name)
    return target
```

### 16.2 fail-closed 原则

- snapshot hash 不匹配 → 拒绝加载，要求 rollback
- event schema 不符 → 跳过该 event，不静默覆盖
- manifest 损坏 → 重建，不返回空
- backup hash 不匹配 → 拒绝 rollback

### 16.3 purge 双确认

```python
def purge(name, yes=False):
    if not yes:
        raise ConfirmationRequired(
            f"This will permanently delete skill '{name}'. "
            "Re-run with --yes to confirm."
        )
    # ...
```

---

## 17. 测试策略

### 17.1 隔离 fixture

```python
@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    config_dir = fake_home / ".config/skillmesh"
    config_dir.mkdir(parents=True)
    # 写测试 config (TOML + JSON 各一份)
    write_test_config(config_dir / "config.toml", ...)
    write_test_config(config_dir / "config.json", ...)
    return SkillmeshEnv(home=fake_home)
```

### 17.2 双机模拟

```python
def test_dual_machine_convergence(isolated_env):
    machine_a = isolated_env.spawn_machine("mac-a")
    machine_b = isolated_env.spawn_machine("mac-b")
    # A scan
    machine_a.scan()
    # 模拟 sync：复制 hub 内容到 B
    machine_b.sync_from(machine_a)
    # B scan
    machine_b.scan()
    # 验证 manifest 一致
    assert machine_a.manifest == machine_b.manifest
```

### 17.3 故障注入

```python
def test_corrupt_event_recovery(isolated_env):
    machine = isolated_env.spawn_machine("mac-a")
    machine.scan()
    # 注入损坏 event
    corrupt_one_event(machine)
    # 重 scan
    machine.scan()
    # 应跳过损坏 event，不崩溃
    assert machine.status_ok()
```

---

## 18. 未尽事项

- WebDAV 内置 sync 客户端（v1.2，坚果云原生支持）
- skill 依赖图（v2）
- 团队共享 hub 权限模型（v2）
- Windows 0.2+ 使用 junction（目录）、symlink（文件）和受管 copy 降级；
  本地 `managed-targets.json` 以 hash 保护 copy，用户修改后 fail-closed。
- single-file layout 多 skill 合并策略（v1.1）
- skill 内容编辑器（不做）
- skill 市场（不做）

详见 PRD §13 Future。
