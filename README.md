# DiskSentry

Windows 本地只读磁盘与文件变更观测台。回答“今天 C 盘又少了几 GB，空间去了哪里”，同时记录 RAM 与进程内存采样。

## 现在能做什么

- **实时流水**：新增、变大、变小、内容/修改时间变化、删除、移动与改名；记录不限文件大小。页面每 2 秒刷新。
- **时间段查询**：今天、昨天、近 7 天、近 30 天；自定义日期，或者精确到分钟的起止时间。日期形式的结束日包含当天；日期时间形式使用半开区间 `[start, end)`。
- **空间对账**：每 15 秒采集磁盘实际可用空间，显示相对首个采样的占用变化、已观察文件净增量及未解释差额。压缩、硬链接、保护区、索引自身开销和缺失事件可能造成差额。
- **找大头**：所选时段增长最多的目录、可搜索分页的文件流水、CSV 导出；默认首页保留彩色嵌套目录面积图、缓存斜纹、候选容量圆环，可切换期间增长地图；大文件索引可折叠。
- **离线清理线索**：首次部署即逐步扫描全部可访问文件，聚合真实缓存容量；分类为可重建、临时文件候选、需核对、建议保留。不用 API，不塞示例容量。
- **RAM**：物理内存曲线，所选时段进程的采样峰值及净变。保留高内存进程和检测到的 Agent 进程；不声称捕获每次内存分配。
- **本地来源证据**：解析 Codex / Pi / Claude 的有限 JSONL 尾部与文件工具路径，给出 Agent、会话 ID、JSONL 路径、工具名和时间，并将证据保存在事件历史中；证据、目录推测、未知明确区分。OpenCode 的非 JSONL 会话存储暂不支持逐会话解析。
- **可选 AI**：从本机 Pi Agent 的 OpenCode Go 配置按需读取凭据，默认 `deepseek-flash`（用户常用别名 dsv4.1f / DeepSeek V4.1 Flash）。没有 API，监控、归类、历史、可视化照常工作。
- **恢复与健康**：登录启动、每分钟当前用户 watchdog、进程内异常恢复、监听器重连、六小时补扫和持久扫描队列。

## 只读边界

**程序没有删除、修改被监控文件或执行任意命令的 API。AI 请求不包含 tools/function calling。**

应用只写入自己的 SQLite 历史、状态、日志以及安装入口。AI 只接收文件元数据、离线规则、会话关联证据和允许读取文件的最多 4 KB 文本片段。凭据文件、`.env`、认证文件、私钥、会话正文、数据库、系统程序和二进制不读取内容；文本片段还会做常见凭据遮罩。

首次全盘扫描不创建 AI 作业，首次扫描完成前自动作业也会等待；手动点击“生成只读摘要”是显式按需请求。之后的实时变更自动排队，最多六次模型请求/分钟。摘要按文件版本缓存，可在设置关闭自动 AI。

“可重建”不是任何时候都可安全删除的保证。先关闭使用缓存的程序，核对离线依赖与唯一数据。会话、工作树、模型、数据库与系统文件不会被当成无条件可删垃圾。AI 的建议也不能越过本地凭据/系统保护规则。

## 运行

要求 Windows 10/11、Python 3.12+。Watchdog 6.0 已随附；唯一额外 Python 依赖是 psutil。

```powershell
git clone https://github.com/Wang-auspicious/DiskSentry.git
cd DiskSentry
python -m pip install -r requirements.txt
.\setup.ps1
python launcher.py
```

安装脚本建立桌面 `DiskSentry` 入口、当前用户登录入口和最低权限的每分钟 watchdog 任务。没有管理员提权、密码保存或系统服务。关闭窗口后继续监控。

暂停并移除后台入口：

```powershell
.\stop.ps1
```

源码、用户文件与历史均保留。后续再运行 `setup.ps1` 恢复后台入口。

## Pi Agent / OpenCode Go

默认读取 `%USERPROFILE%\.pi\agent\models.json` 中 `providers.opencode-go` 的模型与 URL 覆盖，以及 `auth.json` 中 `opencode-go.key`。继承配置未写 URL 时，使用 Pi 内置提供商的 `https://opencode.ai/zen/go/v1`。应用使用自己的 User-Agent 和稳定的 `x-opencode-session`。

凭据不会复制到项目、状态响应、CSV 或 Git。可选环境覆盖：`DISKSENTRY_PI_DIR`、`DISKSENTRY_AI_URL`、`DISKSENTRY_AI_MODEL`；数据目录可用 `DISKSENTRY_DATA` 指定。

## 数据、性能和覆盖

历史位于 `%LOCALAPPDATA%\DiskSentry\index.sqlite3`，日志最多 3 MB，界面显示索引大小。全文件索引会占空间；数百万文件可能占数百 MB 或更多。索引盘低于 5 GB 可用空间时暂停全盘索引，保留实时监听。扫描采用较低进程优先级、有限队列、分批写入与断点续扫；首次扫描需时间，不阻塞流水查询。

本应用单独加载附带的公共领域 SQLite 3.53.1；不替换全局 Python。它修复了旧版本 SQLite 的 WAL-reset 并发问题。若嵌入环境预先加载了旧 SQLite，存储层会使用 rollback journal，避免在受影响版本启用 WAL。

受保护目录、目录链接、挂载点和无权读取路径会跳过。系统关机或监控退出时，不能恢复期间所有中间事件；补扫只能观察仍存在的差异。未知旧大小不会编造为新增字节。进程 RAM 使用不证明其写过某文件；未启用内核审计/驱动，因此不会保证所有写入者均可精确归因。

旧版每日大文件记录能够导入，但旧库保持只读；无法读取的旧索引会报告警告并重新建立元数据。新版本不会把启用前的磁盘变化伪装成已监控历史。

## 开发验证

```powershell
python -m unittest discover -s tests -v
```

覆盖真实 Windows 通知、小文件增长、重命名/删除、时间边界、磁盘对账、无 API、初次扫描零作业、隐私遮罩、只读哈希、来源证据和 API 权限。模块分工：

| 文件 | 职责 |
| --- | --- |
| `collector.py` | 监听、资源采样、全文件扫描与断点续扫 |
| `storage.py` | 元数据、不可变事件流水和时间查询 |
| `rules.py` | 保守离线归类及内容读取边界 |
| `attribution.py` | JSONL 来源关联与置信标记 |
| `ai_analysis.py` | 可选只读 AI 队列与缓存 |
| `service.py` | 受令牌/Host/Origin 约束的本机 API |
| `launcher.py` | 单实例、恢复、安装与历史迁移 |
| `web/v2.*` | 本地 dashboard，无外部资源依赖 |

原创代码为 MIT；第三方许可见 `THIRD_PARTY_NOTICES.txt`、`vendor/LICENSE` 与 `runtime/LICENSE.txt`。
