# 国产网盘部署指南

Skillmesh 不绑定任何特定同步后端。本文档说明如何在坚果云 / 百度网盘 / 夸克网盘 / 阿里云盘上部署。

**核心原理**：所有网盘客户端都提供"同步盘"功能——用户指定一个本地目录，客户端把该目录内容同步到云端及其他机器。skillmesh 只需把 hub 目录放在这个同步目录下即可。

## 通用步骤

1. 安装网盘 Mac 客户端
2. 配置同步盘功能（部分网盘需会员）
3. 把 `config.toml` 的 `hub.path` 指向同步盘目录下的子路径
4. `python3 skillmesh.py init && python3 skillmesh.py scan`

## 坚果云（推荐）

**为什么推荐**：基于 WebDAV，同步最稳定，免费版够用，无文件大小限制。

### 安装

下载 [坚果云 Mac 客户端](https://www.jianguoyun.com/s/downloads) 并登录。

### 配置同步目录

坚果云默认同步目录是 `~/Nutstore Files/`。可在客户端偏好设置中修改。

### skillmesh config

```toml
[hub]
path = "~/Nutstore Files/skillmesh"
sync_backend = "jianguoyun"
```

### 特性

- 文件大小：免费版单文件 100MB（够用，skill 通常 KB 级）
- 同步频率：实时（WebDAV 推送）
- 冲突命名：`foo_conflict.md`
- 占位文件：无（直接全量同步）

## 百度网盘

**注意**：同步盘功能需开通百度网盘会员。

### 安装

下载 [百度网盘 Mac 客户端](https://pan.baidu.com/download)。

### 配置同步盘

1. 登录后打开"同步盘"功能（需会员）
2. 设置同步目录（如 `~/BaiduNetdisk/sync/`）
3. 选择要同步的文件夹

### skillmesh config

```toml
[hub]
path = "~/BaiduNetdisk/sync/skillmesh"
sync_backend = "baidu"
```

### 特性

- 文件大小：会员无限制
- 同步频率：分钟级（非实时）
- 冲突命名：`foo (1).ext`
- 占位文件：`.baidudisk`（未下载时）

### 已知问题

- 大文件分块上传可能导致 hash 校验临时失败 → skillmesh validate 阶段重试
- 同步速度受百度限速影响

## 夸克网盘

**注意**：同步盘功能需开通夸克网盘会员。

### 安装

下载 [夸克网盘 Mac 客户端](https://pan.quark.cn/)。

### 配置同步盘

1. 登录后开通会员
2. 启用"同步盘"
3. 默认同步目录 `~/Quark Drive/`

### skillmesh config

```toml
[hub]
path = "~/Quark Drive/skillmesh"
sync_backend = "quark"
```

### 特性

- 文件大小：会员无限制
- 同步频率：分钟级
- 冲突命名：`foo 冲突副本.ext`
- 占位文件：测试中（默认配置含 `.downloading`）

## 阿里云盘

**注意**：同步盘功能需开通阿里云盘会员。

### 安装

下载 [阿里云盘 Mac 客户端](https://www.aliyundrive.com/)。

### 配置同步盘

1. 登录后开通会员
2. 启用"同步盘"
3. 默认同步目录 `~/Aliyun Drive/`

### skillmesh config

```toml
[hub]
path = "~/Aliyun Drive/skillmesh"
sync_backend = "aliyundrive"
```

### 特性

- 文件大小：会员无限制
- 同步频率：分钟级
- 冲突命名：`foo (1).ext`
- 占位文件：`.downloading`（分块下载中）

### 已知问题

- 大文件分块下载时 `.downloading` 占位文件存在，skillmesh 标 `SYNC-PENDING`
- 偶发同步延迟（特别是网络不稳时）

## 多机部署

每台机器：

1. 安装同一网盘客户端并登录同一账号
2. 启用同步盘，配置相同同步目录
3. clone skillmesh-cli，运行 `init` 生成不同 host UUID
4. 编辑 config 指向同一网盘同步路径
5. `scan` 即可

## 故障排查

### 同步延迟导致 status 显示 SYNC-PENDING

正常现象。等网盘完成同步后再次 `scan`。

### 冲突文件

skillmesh 默认跳过冲突文件并告警。手动合并后删除冲突文件，再 `scan`。

### 大文件 hash 校验失败

某些网盘大文件分块上传/下载期间 hash 可能临时不一致。skillmesh validate 阶段会重试。若持续失败，检查网盘客户端是否完成同步。

### 网盘客户端崩溃

skillmesh 不依赖网盘客户端运行。客户端崩溃只影响同步，本机已物化的 skill 仍可用。重启客户端后下次 scan 自动补齐。

## 自定义冲突/占位规则

默认规则是推测值。如发现新的冲突/占位命名模式，可在 config 覆盖：

```toml
[conflicts]
patterns = [
  "\\(\\d+\\)\\.",
  "冲突",
  "_conflict",
  "\\.conflict$",
  "你的网盘冲突模式",
]

[placeholders]
suffixes = [
  ".icloud",
  ".baidudisk",
  ".downloading",
  ".tmp",
  ".你的网盘占位后缀",
]
```

欢迎把实测规则 PR 给上游。
