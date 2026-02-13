# skillctl

轻量本地 Skill 管理器（单进程，CLI + TUI + Web UI），用于把多 Agent 的技能统一收口到一个 Pool，再按目标分发。

核心模型：

`Source（外部来源） -> Pool（本地统一池） -> Agent（分发目标）`

## 适用场景

- 同时使用 Codex / Claude / Gemini / Antigravity 等多个 Agent。
- 希望统一管理 skills，不再在每个工具里重复拷贝。
- 需要持续追踪 GitHub 等外部来源的技能更新，并按需导入。
- 需要发现 Agent 本地产生的“野生 skill”，再纳入 Pool 统一治理。

## 快速开始

```bash
cd /Users/Simon/Tools/Skills_Pool
python3 skillctl.py init
python3 skillctl.py web
```

默认 Web 地址：`http://127.0.0.1:8765`

## Web 控制台工作流

建议按下面流程使用：

1. 在 `Source 工具` 中新增并登记 GitHub Source。
2. 对 Source 执行“比较差异”，确认新增候选。
3. 选择性导入到 `Pool`（或全量导入）。
4. 在 `同步工具` 把 Pool 中技能分发到目标 Agent。
5. 定期执行“扫描并检查”，将 Agent 本地新增技能纳入 Pool。

GUI 特性：

- 实时日志反馈任务提交、执行进度与结果。
- 无 dry-run（GUI 只执行真实操作）。
- 支持在控制台顶部直接配置网络代理。

## CLI 常用命令

```bash
# 初始化
python3 skillctl.py init

# 扫描本地环境
python3 skillctl.py scan

# 查看内置市场
python3 skillctl.py index --markets

# 索引远程来源
python3 skillctl.py index --source https://github.com/openai/skills.git

# 从来源导入（按 skill 或全量）
python3 skillctl.py fetch --source https://github.com/openai/skills.git --skill skills/foo
python3 skillctl.py fetch --source https://github.com/openai/skills.git --all

# 跟踪来源（新增/检查/导入）
python3 skillctl.py track add --agent deepseek --name "deepseek-skills" --source https://github.com/<org>/<repo>.git
python3 skillctl.py track check --id deepseek-skills
python3 skillctl.py track import --id deepseek-skills --all

# 同步到目标 Agent
python3 skillctl.py sync --prune

# 启动 UI
python3 skillctl.py ui
python3 skillctl.py web --host 127.0.0.1 --port 8765
```

## Source / Pool / Agent 说明

### Source

- 表示远程可追踪来源（当前策略以 GitHub/Git 远程为主）。
- 支持登记、检查快照变化（新增/移除）、按需导入。

### Pool

- 本地统一技能池（单一事实来源）。
- 每个 skill 记录来源元数据（如 `external_source`、`external_rel_dir`）。
- 同名冲突时按哈希判重，不强行覆盖旧版本。

### Agent

- 本地消费端（如 codex / claude / gemini / antigravity / obsidian）。
- 通过同步工具从 Pool 分发技能。
- 本地存在但不在 Pool 的技能可通过 Check 纳入统一管理。

## 生态策略（Ecosystem）

- `follow`：关注哪些具备技能产出能力的来源生态。
- `grant`：把 Pool 技能授予哪些可消费技能的目标生态。
- 同步时会结合 grant 策略过滤目标。

## 网络代理

用于 `index / fetch / track` 等外网请求：

```bash
python3 skillctl.py proxy --set http://127.0.0.1:7897
python3 skillctl.py proxy --no-proxy localhost,127.0.0.1
python3 skillctl.py proxy --clear
```

## 目录结构（默认）

- `skills/`：统一 Pool
- `dist/`：按目标分发视图
- `state/`：扫描、追踪、导入、维护等状态记录
- `index/`：索引缓存
- `trash/`：回收区
- `config/targets.conf`：目标平台映射

## 许可证

当前仓库按项目实际需要维护（如需开源许可证，可补充 `LICENSE` 文件）。
