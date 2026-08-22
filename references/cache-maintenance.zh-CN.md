# CodexCleaner 性能缓存维护（中文）

## 目标与适用条件

诊断并清理 Windows 桌面端 Codex 的可重建浏览器缓存，处理启动缓慢、从其他窗口切回 Codex 时假死、鼠标转圈、输入法被界面卡住或短暂“未响应”等症状。

缓存维护与任务整理是两个独立层级。任务数量少、会话已归档或删除，并不能证明 Chromium、Service Worker、代码或 GPU 缓存处于健康状态。

不要把缓存清理当成定时保养。仅在出现相关症状，或只读审计显示缓存异常膨胀、长期未重建时执行。

## 只读审计

在 Skill 根目录运行：

```powershell
python scripts/cache_maintenance.py audit
python scripts/cache_maintenance.py audit --json
```

报告分别列出 GPU、HTTP、代码、Service Worker 和组件缓存，并统计旧备份。审计不修改任何文件。

诊断性能问题时，同时记录：

- Codex 应用版本；
- 症状发生在启动、任务恢复还是前后台切换；
- 缓存总量及占比最大的目录；
- 任务历史审计结果；
- 清理前后的主观体验和三次焦点切换结果。

不要只依据缓存字节数断言根因。缓存规模、目录年代和可重复症状共同决定是否建议清理。

## 安全边界

清理脚本采用固定允许列表，只移动以下可重建数据类别：

- Chromium HTTP Cache 和 Code Cache；
- GPUCache、ShaderCache、Dawn/WebGPU/Graphite 缓存；
- `codex-browser-app` 分区的 Service Worker 数据；
- Chromium 组件与扩展包缓存。

以下数据必须保留：

- Cookies、登录与认证状态；
- Local Storage、IndexedDB、Session Storage、WebStorage；
- Network 目录及分区中的其他站点状态；
- `$CODEX_HOME` 下的活动任务、归档任务、rollout 会话与数据库；
- 项目目录、Git 仓库、源代码、文档和构建产物。

脚本不直接删除缓存，而是移动到 `%LOCALAPPDATA%\OpenAI\Codex-cache-backups\<时间戳>`，并写入 `manifest.json`。备份至少保留到用户确认登录、任务和性能均正常；通常可在稳定 24–48 小时后另行确认删除。

## 获取确认

实际清理前展示：

- 将移动的缓存目录及总大小；
- 明确保留的数据类别；
- 备份位置；
- Codex 必须完全退出；
- 清理后首次启动会重新生成缓存，可能比平时稍慢一次。

用户确认覆盖当前审计结果。目标集合或大小明显变化时重新审计并重新确认。

## 在应用完全退出后执行

普通子进程会随 Codex 退出而被 Windows 一起终止，不能把“后台脚本已启动”当成成功。使用以下两种方式之一：

1. 让用户从系统托盘彻底退出 Codex，然后在外部终端运行：

   ```powershell
   python scripts/cache_maintenance.py clean --apply --relaunch
   ```

2. 使用可见的 `scripts\run_cache_cleanup.cmd`。它在检测到 `ChatGPT.exe` 仍运行时会拒绝清理，用户退出应用后再次双击即可。

只有在宿主允许创建独立 Windows 任务且用户授权时，才可使用 `--wait-seconds` 让独立进程等待退出。必须先验证任务状态为正在运行、独立进程确实存在；如果任务配置了状态日志，还要确认日志已写出等待阶段。完成后删除准确的单次任务。不要依赖 Codex 自己派生的普通后台进程。

检测到应用仍运行、缓存文件锁定、路径逃逸、符号链接或备份目录与配置目录重叠时，停止并保持剩余数据原状。

## 验证

清理后完成以下复核：

1. `manifest.json` 状态为 `completed`，没有 `failures`；
2. 登录仍有效，活动和归档任务数量未意外变化；
3. 受保护目录仍存在，项目文件未改变；
4. 缓存目录由应用重新创建，体积明显小于清理前；
5. 连续执行至少三次“Codex → 浏览器操作 → Codex”焦点切换；
6. 向用户报告实际移动大小、备份路径和体验结果。

如果清理后数天内再次复发，不要不断重复清理。转向客户端版本、图形栈、应用日志和可复现缺陷排查，并保留最新一次审计与清理清单作为证据。
