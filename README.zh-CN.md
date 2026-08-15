# Codex Provider Sync

[English](README.md)

`codex-switch` 是一个面向恢复场景的 macOS 命令行工具。它用于在切换 Codex model provider 后统一本地聊天元数据，并修复“聊天文件仍在磁盘上，但侧栏显示不完整”的问题。

> [!CAUTION]
> 这是一个非官方 alpha 工具，会操作 Codex 的内部本地状态文件。Codex 可能随版本更新改变这些文件。工具会自动备份、事务写入和校验数据库，但运行前仍应理解下面说明的修改范围。

## 为什么需要它

Codex 的本地聊天历史分布在几层状态中：

```text
~/.codex/sessions 和 archived_sessions
└── 保存对话记录的 JSONL rollout 文件

~/.codex/state_5.sqlite
~/.codex/sqlite/state_5.sqlite
└── 任务元数据和兼容索引

~/.codex/sqlite/codex-dev.db
└── 桌面端左侧任务目录缓存
```

切换 provider 后，这几层的 `model_provider` 可能不一致。受影响时，原始对话仍在 rollout 文件中，但主状态库、兼容状态库或侧栏目录仍指向旧 provider，于是看起来像聊天丢失、标题陈旧，或者续聊被分叉成了新任务。

`codex-switch` 会把这些层作为一次完整操作进行检查和修复。

## 它会做什么

- 统计 rollout、两套线程数据库和侧栏目录中的 provider 分布。
- 将 rollout 的 `session_meta` 同步到 `config.toml` 当前选中的 provider。
- 在两套 `state_5.sqlite` 之间补齐缺失的任务 ID。
- 对同一任务的冲突元数据选择更新时间更晚的记录。
- 修复用户事件可见标记并重建本地侧栏目录。
- 写入前退出 Codex，完成后重新打开。
- 每次修改前创建 SQLite 一致性备份。
- 写入或校验失败时自动回滚。
- 提供备份列表和显式恢复命令。

它**不会**配置 provider 凭据、解密与 provider 绑定的历史，也不会把多个分叉任务合并成一条对话。

## 环境要求

- macOS
- 使用默认 `~/.codex` 本地状态目录的 Codex Desktop
- Python 3.10 或更高版本
- 目标 provider 已经写入 `~/.codex/config.toml`

## 安装

```bash
git clone git@github.com:ZPXing-TAA/codex-provider-sync.git
cd codex-provider-sync
./install.sh
```

安装程序会在 `~/.local/share/codex-provider-sync` 创建隔离虚拟环境，并把一个很小的 shell 启动器安装到 `~/.local/bin/codex-switch`。

确认 `~/.local/bin` 已加入 `PATH`，然后验证：

```bash
codex-switch --version
codex-switch status
```

卸载命令但保留恢复备份：

```bash
./uninstall.sh
```

## 快速开始

先查看当前状态：

```bash
codex-switch status
```

如果 `config.toml` 已经选中了目标 provider，执行：

```bash
codex-switch sync
```

命令会依次：

1. 对包含 `encrypted_content` 的跨 provider 历史发出警告；
2. 退出 Codex；
3. 创建回滚备份；
4. 同步 rollout、两套数据库和侧栏目录；
5. 校验结果数据库；
6. 重新打开 Codex。

如果 provider 已在 `[model_providers.<id>]` 中声明，也可以切换根配置并同步：

```bash
codex-switch switch custom
```

`switch` 会拒绝尚未配置的 provider，不会创建凭据或猜测 API 地址。

## 命令说明

| 命令 | 作用 |
| --- | --- |
| `codex-switch status` | 显示所有历史层的 provider 数量。 |
| `codex-switch status --json` | 输出机器可读的 JSON。 |
| `codex-switch sync` | 同步到 `config.toml` 当前选中的 provider。 |
| `codex-switch switch <provider>` | 切换到已配置 provider 并同步历史。 |
| `codex-switch backups` | 按时间倒序列出回滚备份。 |
| `codex-switch restore [path]` | 恢复指定备份；不传路径时恢复最新备份。 |

常用选项：

- `--yes`：在非交互环境中确认已理解加密历史兼容风险。
- `--keep N`：保留最新 `N` 份备份，默认 5。
- `--no-open`：操作结束后不重新打开 Codex。
- `--codex-home PATH`：操作另一个 Codex home；这是全局选项，应写在子命令前面。

示例：

```bash
codex-switch --codex-home /path/to/test-home status --json
```

## 如何理解 status 数量

示例：

```json
{
  "config_provider": "openai",
  "rollouts": {"openai": 214},
  "databases": {
    "state_5.sqlite": {"openai": 212},
    "sqlite/state_5.sqlite": {"openai": 164}
  },
  "catalog": {"openai": 139}
}
```

总数不同不一定代表数据丢失：

- rollout 包含活动、归档、空会话和内部会话；
- 线程数据库保存交互任务和部分内部任务元数据；
- 侧栏目录只显示未归档且有用户可见内容的对话。

关键是每层内部的 provider 是否一致。`sync` 还会让两套线程数据库包含相同的已知任务 ID。

## 备份与恢复

备份位于：

```text
~/.codex/recovery_backups/<timestamp>-codex-switch/
```

每份备份包含：

- 存在时的 `config.toml`；
- 两套状态库和侧栏数据库的 SQLite 一致性副本；
- 本次将要修改的 rollout 文件；
- 记录来源 provider、目标 provider 和文件路径的 manifest。

查看备份：

```bash
codex-switch backups
```

恢复最新备份：

```bash
codex-switch restore
```

恢复指定备份：

```bash
codex-switch restore ~/.codex/recovery_backups/<timestamp>-codex-switch
```

恢复前，工具还会先备份当前状态，因此恢复操作本身也可以撤销。

## 加密历史限制

部分 rollout 包含由特定 provider 或账号生成的 `encrypted_content`。修改 `session_meta.model_provider` 可以恢复它在目标 provider 下的可见性，但不会转换或解密加密载荷。

因此：

- 对话可能恢复显示，但继续对话时失败；
- 自动压缩可能触发 encrypted-content 校验错误；
- 最终仍可能需要返回原 provider/账号。

确认提示是为了明确这一区别。`--yes` 只是确认你已知风险，不会消除风险。

## 会修改哪些文件

根据本机实际存在的文件，同步可能修改：

```text
~/.codex/config.toml                 # 仅 switch
~/.codex/sessions/**/*.jsonl         # 只修改第一条 session_meta
~/.codex/archived_sessions/*.jsonl
~/.codex/state_5.sqlite
~/.codex/sqlite/state_5.sqlite
~/.codex/sqlite/codex-dev.db
```

安全措施包括：进程锁、配置/rollout 原子替换、SQLite 事务、写前备份，以及写后 `PRAGMA quick_check`。

## 常见问题

### `zsh: killed codex-switch ...`

重新运行 `./install.sh`。安装程序使用 shell 启动器，并将 Python 主程序放在隔离虚拟环境中，从而规避已观察到的 macOS 本地 provenance 执行策略问题。

### `provider 'custom' is not configured`

先在 `~/.codex/config.toml` 中定义 provider。工具不会自动创建 provider 配置或凭据。

### `confirmation required`

非交互运行时检测到了包含加密内容、且 provider 不匹配的 rollout。阅读上面的限制后，如果你确实要修复可见性，再使用 `--yes`。

### Codex 没有重新打开

数据操作可能已经成功。检查终端输出，然后运行：

```bash
open -a Codex
codex-switch status
```

## 开发

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

测试全部使用临时 Codex home，覆盖 provider 校验、TOML 根配置处理、加密历史确认、双库冲突合并、幂等、自动回滚、App 重开和显式恢复。

## 项目状态

本项目解决的是实际恢复问题，但依赖未公开的 Codex Desktop 存储结构。升级到新的 Codex 版本后，建议先对 Codex home 副本进行测试。

本项目与 OpenAI 没有关联，也未获得 OpenAI 官方背书。

## 许可证

[MIT](LICENSE)
