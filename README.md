# 物电 HPC 集群监控 — 私有开发工作区

> 私有仓库，仅开发者本人可见。汇集「物电 HPC 集群监控」项目的安卓 App 源码、集群侧数据采集脚本与交付文档。
> 前端看板本体（GitHub Pages 静态站）在独立公开仓库 [`Leejero/phy-hpc-web`](https://github.com/Leejero/phy-hpc-web)，不包含在本仓库中。

## 项目概览

- **看板网页**：GitHub Pages 静态面板，展示 Slurm 集群的节点、队列、任务与在线用户状态。
- **安卓 App**（`phy-hpc-web-app/`）：Capacitor 8 WebView 外壳，App 内直接渲染看板；多数据源容错取数（jsDelivr → raw.githubusercontent → github.io → 本地快照），断网时优先使用 `localStorage` 中最近一次联网数据。
- **数据链路**：集群侧 cron 定时执行采集脚本 → 脱敏 → 经 GitHub Contents API 推送 `data/slurm.json` 至公开仓库（日间约 20 分钟/次，夜间约 2 小时/次）。

## 目录结构

| 路径 | 说明 |
|------|------|
| `phy-hpc-web-app/` | 安卓 App 工程（Capacitor 8）：`www/` 为 Web 资源源码，`build_debug.sh` 为标准构建入口 |
| `server/` | 集群侧脚本：`data_collector.py`（Slurm 数据采集）、`sync.sh`（脱敏并推送 GitHub）、`setup_cron.sh`（安装定时任务） |
| `slurm_name_map.json` | 用户名脱敏映射表 |
| `物电HPC集群监控-用户手册.{md,pdf}` | 面向使用者的安装与使用手册 |
| `物电HPC集群监控-群公告.{md,png}` | 群内发布公告（最新版） |
| `HPC集群监控-*` | 旧命名版本的公告与手册（历史存档） |
| `phy-hpc-web-代码审查报告.md` 等 | 看板前端的代码审查 / 界面优化 / 整改文档 |
| `脱敏部署说明.md` | 采集端脱敏与部署说明 |

## 安卓 App 构建

```bash
cd phy-hpc-web-app
bash build_debug.sh
```

脚本三步合一：① 用本地全量仓库最新 `slurm.json` 刷新编译期快照；② 同步 `www/` → `android/app/src/main/assets/public/`；③ Gradle 前台编译 `assembleDebug`。
产物：`android/app/build/outputs/apk/debug/app-debug.apk`（发布时重命名为 `物电HPC集群监控.apk`）。

环境要求：JDK 21（Android Studio 自带 jbr）、Android SDK compileSdk 36、Gradle 8.14.3（wrapper 已指向本地离线 zip）。

> 注意：不要绕过 `build_debug.sh` 直接裸跑 `gradlew`，否则编译期离线快照不会刷新。

## 集群侧部署

1. 将 `server/` 上传至集群登录节点；
2. `bash setup_cron.sh` 安装定时任务；
3. 需要立即推送一次数据时执行 `bash sync.sh`。

## 忽略项说明

`.gitignore` 已排除：`node_modules/`、本地 Android SDK、Gradle 离线包、构建产物（`build/`、`.gradle/`）、IDE 配置，以及两个嵌套的独立公开仓库 `phy-hpc-web/`、`phy-hpc-web-full/`（它们各自在 GitHub 上维护）。
