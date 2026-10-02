# 离线软件成分分析原型

该项目先归并离线依赖证据，建立项目版本、规范组件身份、来源清单和构建产物路径之间的关系。漏洞资料、网络查询和许可证策略留到后续阶段。

## 身份规则

- 组件身份由生态系统和规范名称组成，并生成不带版本的 Package URL。
- 只比较显示名称不会合并：`pypi/requests` 与 `npm/requests` 是两个组件。
- Python 名称按 PEP 503 归一化：连续的 `-`、`_`、`.` 都会变成单个 `-`。因此 `PyYAML` 与 `pyyaml` 指向同一组件，而 `Py.YAML` 归一化为 `py-yaml`，是另一个组件；名称相近不构成合并理由。
- 清单显式给出的同生态别名会自动按同一逻辑组件分组（例如 Maven group 迁移后的旧坐标），但随时可以人工撤回；人工新增别名时应先做影响预览再批准。跨生态或身份不存在的别名会被拒绝。
- 版本冲突在项目内标记为 `conflict`；版本一致的多条证据标记为 `unified`。
- 每条证据保留来源清单、原始 JSON、依赖类型、请求者、scope 和构建产物路径。
- 人工别名支持影响预览、批准与撤回。批准只改变逻辑分组，不删除组件和证据；撤回后原始组件关系与全部出现证据恢复。别名带来的版本冲突仍标记为 `conflict`，不会因为名称相近而消失。
- 每次导入或别名变更都会保存项目扫描结论快照，撤回别名后仍可查看历史扫描结论。

## 演示场景

`samples/` 中包含同一个项目 `shopstack 2.0.0` 的四份清单：

- Python `requests` 在两个后端产物中重复出现，版本一致，归并为一个组件但保留两条路径。
- Python `urllib3` 分别为 `2.2.1` 和 `2.3.0`，保留冲突。
- npm `debug` 在不同 `node_modules` 嵌套层级中版本不同，保留冲突和两个路径。
- npm `lodash` 在两个嵌套路径中版本一致，明确展示“合并身份但不合并证据路径”。
- Maven `com.example:platform-core` 与旧坐标 `com.example.legacy:platform-core` 通过别名合并。
- PyPI 与 npm 都有显示名 `requests`，但不会跨生态合并。
- PyPI `PyYAML`（归一化 `pyyaml`）与遗留包 `Py.YAML`（归一化 `py-yaml`）仅名称相近，保持为两个组件，不会误合并。

## 使用

```bash
python -m sca.cli import --db /tmp/sca.sqlite3 samples/*.json
python -m sca.cli report --db /tmp/sca.sqlite3 --project shopstack
python -m sca.cli report --db /tmp/sca.sqlite3 --project shopstack --json
# 预览人工别名的影响（不写库）
python -m sca.cli alias preview --db /tmp/sca.sqlite3 \
  --from maven:com.example:platform-core --to maven:com.example.legacy:platform-core
# 或先提议为待批准，再批准
python -m sca.cli alias propose --db /tmp/sca.sqlite3 \
  --from maven:com.example:platform-core --to maven:com.example.legacy:platform-core
# 批准人工别名（撤回使用 alias revoke）
python -m sca.cli alias approve --db /tmp/sca.sqlite3 \
  --from maven:com.example:platform-core --to maven:com.example.legacy:platform-core
# 查看历史扫描结论
python -m sca.cli history --db /tmp/sca.sqlite3 --project shopstack
```

## 受限别名决策（与导入、报告、历史同一入口）

人工别名提案可以携带显式作用域：生态系统、版本范围和来源清单路径。决策只在其作用域内生效，
不会扩散到其他项目或来源；所有提案、批准、撤回都写入同一个 SQLite 数据库，
report 与 history 立即可见，无需切换入口或存储。

```bash
# 带作用域的提案（--source 可重复；默认 versions=*、来源=*）
python -m sca.cli alias propose --db /tmp/sca.sqlite3 \
  --from maven:com.a:core --to maven:com.b:core \
  --versions ">=1.0.0,<2.0.0" --source samples/services.maven-tree.json
# 查看所有受限决策及其状态
python -m sca.cli alias decisions --db /tmp/sca.sqlite3
# 多份待批准提案的联合预览：组合后若把不兼容版本连在一起，
# 会列出冲突经过的提案与原始证据路径（来源清单 :: 构建路径）
python -m sca.cli alias joint-preview --db /tmp/sca.sqlite3
# 只采纳其中一部分；剩余提案按新的已确认范围重新预览，作用域不会被扩大
python -m sca.cli alias adopt --db /tmp/sca.sqlite3 --id 1 --id 3
# 记录未采用原因
python -m sca.cli alias reject --db /tmp/sca.sqlite3 --id 2 --reason "两个组件身份不同"
# 撤回已采纳提案：重新核对相关待批准提案；批准时的依据与未采用原因保留
python -m sca.cli alias withdraw --db /tmp/sca.sqlite3 --id 1
```

说明：嵌套的 sca/sca/ 目录是最初的独立原型（JSONL 事件日志），其能力已整合进上述主包；
保留原样仅供对照，新工作请使用仓库根目录的 python -m sca.cli。

重复导入同一内容不会新增证据：来源内容使用 SHA-256 去重，单条记录还有独立的规范化哈希（包含生态、规范身份、版本与构建路径）。导入同一路径的更新内容时，会作为项目的新来源版本保留；导入不同路径但内容完全相同的副本、以及同一路径的重复导入都不会产生重复证据。组件去重通过规范身份在全库范围内追溯，跨构建路径出现的同一组件只归并身份、不合并证据。

## 测试

```bash
python -m unittest discover -s tests -v
```
