# HPC Task Status

独立的计算子任务状态数据仓库，供个人安卓监控 App 读取。

本仓库与现有集群监控网页、原安卓 App 相互独立，不向它们提供数据。

## 数据文件

App 只读取 `data/status.json`，使用 schema 2 紧凑格式。顶部统计全部子任务，明细区块只包含**当前计算 slide**：实际运行的 RELAX/SCF 任务、最多 30 条该 slide 的失败/警告/未知记录，以及 5 条该 slide 的最近成功记录。跨 slide 的历史完成记录保留在采集端本地，不进入公开列表；等待中的子任务仅计数。

`scope` 由当前 VASP 进程工作目录确定；进程短暂未启动时，回退到最近更新的 `run.log` 中最后一条事件所属 slide。`run.log` 只含时分秒，采集端从其时间顺序反向识别跨天边界，重建每条事件的日历日期；缺少日志事件时才以对应 RELAX/SCF 输出文件的修改时间为兜底。已从 `pairs.tsv` 移除的任务也会从本地历史中清除。

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

采集端位于服务器用户主目录顶层的 `server/`，每次动态读取 `pairs.tsv`，不固定子任务数量。`tasks.json` 的私有路径与真实任务结构不上传。

- `collect_status.py`：检查 VASP 收敛与能量记录，当前目录优先，历史目录仅补齐当前未开始的结果；
- `push_status.py`：校验紧凑格式及公开字段，推送后回读验证；
- `sync.sh`：加锁采集并推送，失败时保留本地快照；
- `install_cron.sh`：只更新本监控的标记区块，保留其他定时任务。
