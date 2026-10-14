# Codex Provider Sync

[English](README.md)

`codex-switch` 是一个面向 macOS Codex Desktop / ChatGPT 中 Codex 的非官方工具。**先在 CC Switch 切换服务，再运行 `codex-switch sync`。** 工具协调旧聊天的请求路由和本地索引，不压缩、不删除、不重写原生聊天历史。

两边使用同一个命令，不需要指定 `openai` / `custom`，也不需要 `--yes`。命令会关闭并重新打开桌面应用；请先等其他任务完成。

> [!CAUTION]
> **请停止在真实数据上使用 0.3.2 及更早版本。** 这些版本可能删除 provider 原生 response item、把所有线程重标为同一个 provider，或在恢复时用旧数据库快照覆盖较新的聊天。请保留 `~/.codex/recovery_backups/` 下的全部现有备份，先升级；排查期间不要运行旧版 `sync` 或 `restore`。旧版本已删除的历史无法由本项目凭空重建；只能从最完整、未被改写的备份中恢复。

## 0.5 的重要变化

- `sync` 跟随 CC Switch 已选好的配置，不会再切一次 provider，也不修改 `auth.json`。支持 `openai`、`custom`、`openai-http` 这组 provider。
- 内置 `openai` 在此工作流中表示官方服务；选中它时会清除 `openai_base_url`。中转站请使用带明确 `base_url` 的 `custom` 配置。若 CC Switch 删除了旧聊天仍需使用的 `custom` / `openai-http` 定义，工具会补回指向当前服务的别名。
- 当选中中转站时，使用官方支持的 `openai_base_url` 把旧 OpenAI 聊天的请求发往当前中转地址；其他已配置的 `custom` / `openai-http` 路由也跟随当前服务。切回官方时清理工具留下的中转地址，并替换旧路由中残留的中转 token。不会把旧聊天重标成新 provider。
- 因此 `sync` 会更新配置里的上述路由定义，覆盖非当前侧的配置别名。服务地址和登录由 CC Switch 管理；下次切换仍先用 CC Switch。完整修改前配置会保存在备份中。
- 自定义 endpoint 必须配有 CC Switch 写入 `auth.json` 的匹配 API key。使用独立 `env_key`、认证命令或额外请求头的复杂配置不能自动映射到内置 OpenAI provider，工具会在修改前报错。
- 同时识别旧 `Codex.app` 和新版 `ChatGPT.app`，确保重启后重新加载配置。

0.4 引入的数据保护机制继续保留：

- `switch` 只修改根级 `model_provider`，并完整备份配置、数据库和全部 rollout 文件。它**不会**改写 JSONL response item、reasoning、compaction、工具状态、线程来源信息或侧栏索引。
- `switch` 不带 provider 参数时，会在 `openai` 和 `custom` 之间切换，额外的 `openai-http` 等配置不会阻止切换。从 `openai-http` 执行时切到 `custom`。没有名为 `custom` 的 provider 时，仅在恰好配置一个非 OpenAI provider 的情况下自动切换；其余情况须明确指定目标。
- `sync` 的索引协调仍按每个 rollout 自己记录的 provider provenance 修复能明确匹配的数据库记录；保留混合 provider 状态以及无法匹配的侧栏条目。
- `restore` 采用增量合并：补入缺少的 rollout 和数据库记录，同时保留当前及新增数据。若能识别出精确前缀或旧版规范化造成的已知快照差异，可恢复规范原件并接回明确无歧义的新增后缀；冲突不明确时会保留现状并报告，不会强行覆盖。
- 恢复备份只还原根级默认 provider，不会替换当前整份配置。每次恢复前都会先备份当前完整状态，包括所有 rollout。
- 默认不清理备份。只有显式指定 `--keep N` 才会清理新版备份，旧版及 recovery 备份不会被删除。

history normalizer 仍可作为有意构造请求的宿主代码中的独立库边界；CLI 不会用它改写磁盘上的历史，它也不是自动请求序列化器。

## 重要限制

这次修复解决的是“中转 key 被发给官方接口”的路由错配，不保证任意服务间的原生状态兼容。Provider 原生 response ID、加密 reasoning、compaction 和 continuation lineage 可能依赖原来的账号、endpoint 或 transport；工具会保留这些内容，而不会删除它们来强行续聊。

旧 `openai` 聊天仍使用内置 provider 的传输能力，包括 WebSocket；仅支持 HTTP 的中转站可能仍无法继续这些聊天。如果发送出现 encrypted content / lineage 错误，请保留原件和备份，并切回原服务，不要运行旧版压缩或恢复命令。`sync` 成功表示配置与索引处理完成，不表示已在真实服务上验证续聊。

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

升级已有安装：拉取新版并重新运行 `./install.sh`。使用 CC Switch 路由跟随功能前，请确认 `codex-switch --version` 显示 0.5.0 或更高版本。

## 命令

| 命令 | 作用 |
| --- | --- |
| `codex-switch status [--json]` | 查看 provider provenance 与索引数量。 |
| `codex-switch switch [provider]` | 不带参数时在 `openai`/`custom` 之间切换；也可明确指定目标。 |
| `codex-switch sync` | 跟随 CC Switch 当前路由，协调索引，不改写历史。 |
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

OpenAI 是内置 provider；其他 provider 必须已在 `~/.codex/config.toml` 声明。工具不会创建 provider 凭据。

**使用 CC Switch 时不要随后运行 `switch`。** 那会再次修改默认 provider，且不会切换认证。`switch [provider]` 只保留给手动管理配置的用户；它不是 CC Switch 的登录切换替代品。选择当前 provider 时不会重启应用或再备份。

如果仍出现 `401 / invalid_api_key`，核对错误里的请求地址是否是 CC Switch 当前选中的服务。地址正确但 key 被拒绝时，需要检查该服务的 key / 登录状态；本工具不会生成或替换凭据。

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

可选的原生路由验证（需要 Node.js 和本机 Codex CLI）仅使用临时数据、测试 key 和本地回环服务，不调用真实模型：

~~~bash
CODEX_BINARY=/path/to/codex node tests/route_probe.mjs
~~~

## 许可证

[MIT](LICENSE)
