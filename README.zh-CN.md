# Codex Provider Sync

[English](README.md)

`codex-switch` 是一个面向 macOS Codex Desktop 的非官方恢复工具。它可以切换默认 provider，但不会重写聊天中的 provider 原生历史记录。Codex 本地存储格式未公开；升级 Codex 后请先在副本上验证，并始终保留备份。

> [!CAUTION]
> **请停止在真实数据上使用 0.3.2 及更早版本。** 这些版本可能删除 provider 原生 response item、把所有线程重标为同一个 provider，或在恢复时用旧数据库快照覆盖较新的聊天。请保留 `~/.codex/recovery_backups/` 下的全部现有备份，先升级；排查期间不要运行旧版 `sync` 或 `restore`。旧版本已删除的历史无法由本项目凭空重建；只能从最完整、未被改写的备份中恢复。

## 0.4.0 的重要变化

- `switch` 只修改根级 `model_provider`，并完整备份配置、数据库和全部 rollout 文件。它**不会**改写 JSONL response item、reasoning、compaction、工具状态、线程来源信息或侧栏索引。
- `switch` 不带 provider 参数时，仅在配置中恰好有一个 `openai` 和一个非 OpenAI 的 custom provider 时自动切换；否则必须明确指定目标。
- `sync` 只做索引协调：按每个 rollout 自己记录的 provider provenance 修复能明确匹配的数据库记录；保留混合 provider 状态以及无法匹配的侧栏条目。
- `restore` 采用增量合并：补入缺少的 rollout 和数据库记录，同时保留当前及新增数据。若能识别出精确前缀或旧版规范化造成的已知快照差异，可恢复规范原件并接回明确无歧义的新增后缀；冲突不明确时会保留现状并报告，不会强行覆盖。
- 恢复备份只还原根级默认 provider，不会替换当前整份配置。每次恢复前都会先备份当前完整状态，包括所有 rollout。
- 默认不清理备份。只有显式指定 `--keep N` 才会清理新版备份，旧版及 recovery 备份不会被删除。

history normalizer 仍可作为有意构造请求的宿主代码中的独立库边界；CLI 不会用它改写磁盘上的历史，它也不是自动请求序列化器。

## 重要限制

Provider 原生 response ID、加密 reasoning、compaction 状态、工具调用状态和 continuation lineage 可能只适用于生成它们的 provider、账号、endpoint 或 transport。切换 provider 无法让旧聊天自动变成可移植历史，也无法修复 Codex 已拒绝的 lineage。旧聊天请继续使用原 provider；切换后需要新 provider 时，请新建聊天。`sync` 成功不代表旧聊天支持跨 provider 续聊。

## 本地存储与 status

Codex 的本地数据分布在 JSONL rollout、线程数据库（`state_5.sqlite` 和 `sqlite/state_5.sqlite`）及侧栏目录数据库（`sqlite/codex-dev.db`）。`status` 会分别统计这些层：

- `config_provider` 是新请求的默认 provider，不代表每个旧聊天都属于它。
- `rollouts` 统计文件数，包含活动、归档、空会话和内部会话。
- `databases` 统计索引中的线程记录；两个数据库位置不等于每个聊天有两份副本。
- `catalog` 统计侧栏记录，数量不必与 rollout 或线程索引相同。

各层数量不同本身不代表数据丢失。安全切换后同时看到多个 provider 是正常的；把所有计数改成 `custom` 并不是修复目标。

~~~bash
codex-switch status
codex-switch status --json
~~~

## 安装

需要 macOS、Codex Desktop 和 Python 3.10 或更新版本。自定义 provider 应事先写入 `~/.codex/config.toml`。

~~~bash
git clone git@github.com:ZPXing-TAA/codex-provider-sync.git
cd codex-provider-sync
./install.sh
codex-switch --version
~~~

安装程序会在 `~/.local/share/codex-provider-sync` 创建隔离环境，并将命令安装到 `~/.local/bin`。请确认此目录已加入 `PATH`。卸载命令不会删除恢复备份：

~~~bash
./uninstall.sh
~~~

升级已有安装：拉取新版并重新运行 `./install.sh`。执行任何修复前，请确认 `codex-switch --version` 显示 0.4.0 或更高版本。

## 命令

| 命令 | 作用 |
| --- | --- |
| `codex-switch status [--json]` | 查看 provider provenance 与索引数量。 |
| `codex-switch switch [provider]` | 修改默认 provider；不带参数时仅在恰好一个 OpenAI + 一个 custom provider 时自动切换。 |
| `codex-switch sync` | 按 rollout 各自记录的来源协调索引，不改写历史。 |
| `codex-switch backups` | 列出恢复备份。 |
| `codex-switch restore [path]` | 增量恢复备份；默认选择最新备份并保留当前/新增数据。 |

选项：

- `--codex-home PATH` 是全局参数，应放在命令前，可用于操作测试用 Codex home。
- 写入前通常会退出 Codex；`--no-open` 表示完成后不重新打开。
- `--keep N` 显式启用新版 `switch`/`sync` 备份清理；旧版及 recovery 备份受保护。

先在 Codex 或 CC Switch 中选好目标，然后运行：

~~~bash
codex-switch sync
~~~

也可以直接选择目标：

~~~bash
codex-switch switch custom
~~~

OpenAI 是内置 provider；其他 provider 必须已在 `~/.codex/config.toml` 声明。工具不会创建 provider 凭据。

## 备份与恢复

备份保存在 `~/.codex/recovery_backups/<timestamp>-codex-switch/`。新版每次修改前都会完整备份当前配置、所有 rollout 文件及 SQLite 一致性数据库副本。`restore` 会合并缺少的数据，而不是用旧快照替换当前状态。若提示存在不明确冲突，请停止并检查两份数据，不要强行覆盖。

如果旧版本已经生成压缩过的 rollout，较早备份中仍可能保留完整原件。请保留所有备份并先对照检查。不要用旧版 `restore`“再试一次”。

先只读列出候选备份：

~~~bash
codex-switch backups
~~~

检查后再指定备份路径；最新备份也可能只是恢复前的安全快照。请先复制 Codex home 到独立目录，在副本上试恢复：

~~~bash
codex-switch --codex-home /path/to/test-home restore /path/to/candidate-backup --no-open
~~~

检查恢复出的文件、冲突和索引后，再决定是否对真实数据操作。安全备份能保留恢复前状态，但不能让模糊的历史冲突自动变得可合并。

## 开发

~~~bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
~~~

测试使用隔离的临时 Codex home。本项目依赖未公开的 Codex Desktop 存储结构，与 OpenAI 无关联，也未获其官方背书。

## 许可证

[MIT](LICENSE)
