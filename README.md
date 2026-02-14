# skillctl

轻量本地 Skill 管理器（单进程，CLI + TUI + Web UI），用于把多 Agent 的技能统一收口到一个 Pool，再按目标分发。  
A lightweight local skill manager (single-process, CLI + TUI + Web UI) that centralizes skills into one Pool and distributes them to target agents.

核心模型 / Core model:  
`Source (External) -> Pool (Local Registry) -> Agent (Deployment Targets)`

## 中文说明

### 适用场景

- 同时使用 Codex / Claude / Gemini / Antigravity 等多个 Agent。
- 希望统一管理 skills，不再在每个工具里重复拷贝。
- 需要追踪 GitHub 等外部来源的技能更新，并按需导入。
- 需要发现 Agent 本地产生的技能并纳入 Pool 统一治理。

### 快速开始

```bash
cd /Users/Simon/Tools/Skills_Pool
python3 skillctl.py init
python3 skillctl.py web
```

默认 Web 地址：`http://127.0.0.1:8765`

### Web 控制台工作流

1. 在 `Source 工具` 新增并登记 GitHub Source。
2. 对 Source 执行“比较差异”，确认新增候选。
3. 选择性导入到 `Pool`（或全量导入）。
4. 在 `同步工具` 将 Pool 技能分发到目标 Agent。
5. 定期执行“扫描并检查”，把 Agent 本地新增技能纳入 Pool。

### 常用 CLI 命令

```bash
# 推荐新命令组（Source -> Pool -> Agent）
python3 skillctl.py status
python3 skillctl.py source markets
python3 skillctl.py source add --agent deepseek --name "deepseek-skills" --source https://github.com/<org>/<repo>.git
python3 skillctl.py source compare --id deepseek-skills
python3 skillctl.py source import --id deepseek-skills --all
python3 skillctl.py pool list --limit 20
python3 skillctl.py agent list
python3 skillctl.py agent sync --prune

# 仍保留兼容旧命令（track/index/fetch/sync/promote）
python3 skillctl.py track list --all
python3 skillctl.py index --markets
python3 skillctl.py fetch --source https://github.com/openai/skills.git --all
```

### 网络代理

```bash
python3 skillctl.py proxy --set http://127.0.0.1:7897
python3 skillctl.py proxy --no-proxy localhost,127.0.0.1
python3 skillctl.py proxy --clear
```

### 目录结构

- `skills/`: 统一 Pool
- `dist/`: 按目标分发视图
- `state/`: 扫描、追踪、导入、维护状态
- `index/`: 索引缓存
- `trash/`: 回收区
- `config/targets.conf`: 目标平台映射

## English

### Use Cases

- You use multiple coding agents (Codex / Claude / Gemini / Antigravity).
- You want one shared skill registry instead of per-tool copying.
- You need to track external skill sources (e.g. GitHub) and import selectively.
- You want to discover locally created agent skills and promote them into a managed pool.

### Quick Start

```bash
cd /Users/Simon/Tools/Skills_Pool
python3 skillctl.py init
python3 skillctl.py web
```

Default Web URL: `http://127.0.0.1:8765`

### Web Workflow

1. Add and register a GitHub source in `Source Tools`.
2. Run source comparison to identify new candidates.
3. Import selected skills (or import all).
4. Sync Pool skills to selected target agents.
5. Run periodic scan/check and promote unmanaged local skills into the Pool.

### Common CLI Commands

```bash
# Recommended command groups (Source -> Pool -> Agent)
python3 skillctl.py status
python3 skillctl.py source markets
python3 skillctl.py source add --agent deepseek --name "deepseek-skills" --source https://github.com/<org>/<repo>.git
python3 skillctl.py source compare --id deepseek-skills
python3 skillctl.py source import --id deepseek-skills --all
python3 skillctl.py pool list --limit 20
python3 skillctl.py agent list
python3 skillctl.py agent sync --prune

# Legacy commands are still supported (track/index/fetch/sync/promote)
python3 skillctl.py track list --all
python3 skillctl.py index --markets
python3 skillctl.py fetch --source https://github.com/openai/skills.git --all
```

### Network Proxy

```bash
python3 skillctl.py proxy --set http://127.0.0.1:7897
python3 skillctl.py proxy --no-proxy localhost,127.0.0.1
python3 skillctl.py proxy --clear
```

### Directory Layout

- `skills/`: unified Pool
- `dist/`: per-target distribution views
- `state/`: scan/track/import/maintenance state
- `index/`: indexing cache
- `trash/`: recycle area
- `config/targets.conf`: target platform mapping

## License

如需开源许可证，请补充 `LICENSE` 文件。  
Add a `LICENSE` file if you want to open-source this project.
