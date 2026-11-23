# HPC Task Status

独立的计算子任务状态数据仓库，供个人安卓监控 App 读取。

本仓库与现有集群监控网页、原安卓 App 相互独立，不向它们提供数据。

## 数据文件

App 只读取 `data/status.json`，使用 schema 2 紧凑格式。**统计范围仅为当前运行中任务所在 slide 里已准备运行的子任务**：采集器先由本用户 VASP 进程的实际工作目录定位所属 `pairs.tsv` 任务树，再只取该进程所在的 slide 目录，最后仅纳入 `INCAR`/`KPOINTS`/`POSCAR`/`POTCAR` 已齐备的距离子目录；同一任务树中其他 slide、归档、旧版本、备份目录以及只有残留输入、尚未准备运行的距离目录一律不计入 `total`。进程短暂不可见时（作业间隙），依次用 `tasks.json` 的私有 `batch_root` 提示、最近更新的运行日志、最近更新的 `pairs.tsv` 回退，不写死任何文件夹。

明细区块只包含**当前计算 slide**：实际运行的 RELAX/SCF 任务、最多 30 条该 slide 的失败/警告/未知记录，以及 5 条该 slide 的最近成功记录。跨 slide 的历史完成记录保留在采集端本地，不进入公开列表；等待中的子任务仅计数。公开记录中的 `group` 是可读批次名（如 `2H · fb_pass_submit`），只含 case 与任务树目录名，不含完整路径。

**完成记录的时效**：`issues` 与 `recent_success` 只收录相对本次采集时间 24 小时内完成、且能判定完成时间的记录；无法判定时间或早于该窗口的记录只计入 `counts`，不进入公开明细。`push_status.py` 会逐条复核每条完成记录的 `time` 必须落在 `[updated_at - 24h, updated_at + 5min]`，并拒绝过期快照，防止旧记录被推送。

`scope` 由当前 VASP 进程工作目录确定；进程短暂未启动时，回退到**当前作业目录**中最近更新的运行日志（匹配 `run*.log`，兼容 `run.log`、`run_v6.log` 等）里最后一条事件所属 slide；再无信息时才取近 24 小时内完成记录所属 slide。`inherit` 归档目录的旧日志只用于给继承来的输出文件标注完成时间，**绝不参与当前 slide 判定**，避免集群空闲时把十几天前的旧 slide 当成当前范围。运行日志通常只含时分秒，采集端从其时间顺序反向识别跨天边界，重建每条事件的日历日期；缺少日志事件时才以对应 RELAX/SCF 输出文件的修改时间为兜底。已从 `pairs.tsv` 移除的任务也会从本地历史中清除。

App 端同时拉取 GitHub Raw 与 jsDelivr，并按 `updated_at` 选取**最新**快照，避免 CDN 落后数小时时把旧数据当成当前结果；本地缓存也只在快照不比现有缓存更旧时更新。解析层会再次丢弃超出 24 小时窗口或无有效日期的完成记录，并对超过 26 小时的快照给出「数据可能已过期」提示。

```json
{
  "schema_version": 2,
  "updated_at": "2026-11-05T16:00:00+08:00",
  "timezone": "Asia/Shanghai",
  "collector_status": "ok",
  "job_active": true,
  "total": 2,
  "counts": {
    "success": 1,
    "warning": 1,
    "failed": 0,
    "running": 0,
    "pending": 0,
    "unknown": 0
  },
  "scope": ["slide-001"],
  "running_tasks": [
    {
      "name": "slide-001/2.00",
      "group": "2H",
      "status": "running",
      "phase": "relax",
      "detail": "弛豫计算进行中"
    }
  ],
  "issues": [
    {
      "name": "slide-001/1.90",
      "group": "2H",
      "status": "warning",
      "phase": "relax",
      "detail": "正常退出但未收敛",
      "time": "2026-11-05T15:01:32+08:00"
    }
  ],
  "recent_success": [
    {
      "name": "slide-001/2.10",
      "group": "2H",
      "status": "success",
      "phase": "scf",
      "detail": "计算成功",
      "time": "2026-11-05T15:26:44+08:00"
    }
  ]
}
```

| 状态 | 判定 |
| --- | --- |
| `success` | 弛豫收敛、电子自洽收敛且能量记录为单个有限数值 |
| `warning` | 正常退出但未收敛，或能量等完整性检查未通过 |
| `failed` | 明确异常终止或非零退出码 |
| `running` | 本用户 VASP 进程的实际工作目录对应该子任务的 RELAX 或 SCF 目录 |
| `pending` | 尚未开始或等待下一阶段 |
| `unknown` | 已有输出但状态证据不足 |

状态是计算程序收敛与完整性的技术判定，**不以 Slurm 的 `COMPLETED` 或进程退出码 0 直接判定成功**，也不代替对物理模型和科学结论的人工审查。

## 公开与隐私约束

- 子任务按用户要求使用 `slide-000/2.10` 的真实标识，不做别名替换。
- 不上传服务器名称、用户名、IP 地址、文件路径、输入输出文件、日志或科研数据。
- 完整完成记录只保存在采集端 `data/history.json`，不会推送到本仓库。
- GitHub 凭据仅保存在采集端 `github.env`，权限为 600，不进入仓库或 App。
- 本仓库为公开仓库，新增字段前必须确认可以公开。

## 推送计划

采集端按北京时间每天 08:00、10:00、12:00、14:00、16:00、18:00、20:00 更新，共 7 次。

采集端位于服务器用户主目录顶层的 `server/`。`tasks.json` 只声明 `discover: true` 与 `search_roots: ["auto"]`（默认取用户的 `/scratch/<user>` 与家目录），**不列出任何任务文件夹**：每次采集先在该范围内发现全部 `pairs.tsv` 任务树，再按当前 VASP 进程的工作目录选出正在跑的那一棵，并按真实的 `OUTCAR` 位置匹配子任务。因此任意目录层级与命名（如 `relax/slide-000/d-2.10/f1_1/norm`、`retry/work/slide-000/d-1.70`、单阶段的 `2H/s000/d-3.80`）都能被识别，任务数量与目录动态变化，不写死清单。可选的私有字段 `batch_root` 用于进程不可见时人工指定当前批次。

兼容两类任务清单：表头 `# slide distance direction stacking` 的两阶段（RELAX+SCF）布局，以及 `# case slide d_HCu` 的单阶段布局（对应 `[MM-DD HH:MM] OK/FAIL/SKIP <slide> d-<distance> rc=<n>` 日志）。完整历史只留在采集端，不推送到本仓库。

- `collect_status.py`：检查 VASP 收敛与能量记录，当前目录优先，历史目录仅补齐当前未开始的结果；
- `push_status.py`：校验紧凑格式及公开字段，推送后回读验证；
- `sync.sh`：加锁采集并推送，失败时保留本地快照；
- `install_cron.sh`：只更新本监控的标记区块，保留其他定时任务。
