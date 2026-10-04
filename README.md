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
- 人工别名可限定作用范围：生态系统（始终限定）、版本范围（如 `>=1.0,<2.0`）与来源项目列表。范围外的项目和版本不会被合并；批准沿用提案已确认的范围，重新提议不会悄悄扩大范围，必须先撤回再重新提议。
- 多份待批准提案支持联合预览：单条看似合法的提案组合后可能把不兼容版本连进同一组，预览会指出冲突经过哪些提案以及涉及的原始来源路径。可以只批准一部分提案，剩余提案按新的已确认范围自动重新预览；也可以带原因拒绝提案。
- 撤回已批准别名后，会自动重新核对与之关联的待批准提案；批准时记录的依据（note）与未采用原因（reject reason）都会保留。
- 每次导入或别名变更都会保存项目扫描结论快照，撤回别名后仍可查看历史扫描结论。
- 导入同一项目的新版本时，旧版本批准不会自动扩大到新版本：系统会生成逐项继承审阅，并列出每个决定影响的新证据。用户可以完整继承、用新版本范围缩小后继承，或明确不继承；未完成审阅期间，新版本按原始组件身份聚合。
- 来源清单变化、版本变化以及迁移到另一生态系统会分类展示。生态系统或规范身份变化时不能继承旧批准；相同显示名仍按生态系统和规范名称区分。继承决定只向当前项目版本和当前证据来源追加作用域，旧版本的批准、证据和历史报告仍独立可读。
- 继承审阅可保存后重新打开；撤回已继承或已缩小的单项决定会移除该版本的新增作用域并重新聚合，但不会删除或改写旧版本依据。

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
# 限定别名作用的版本范围与来源项目
python -m sca.cli alias propose --db /tmp/sca.sqlite3 \
  --from pypi:pyyaml --to pypi:py-yaml --versions '>=6.0' --source shopstack \
  --note 'legacy vendor rename'
# 联合预览全部待批准提案（或 --edges 1,2 指定子集）
python -m sca.cli alias preview --db /tmp/sca.sqlite3
# 只批准一部分提案，剩余提案按新的已确认范围重新预览
python -m sca.cli alias approve --db /tmp/sca.sqlite3 --edges 2,3 --note 'confirmed'
# 带原因拒绝提案（原因会保留）
python -m sca.cli alias reject --db /tmp/sca.sqlite3 --edge-id 4 --reason 'distinct components'
# 撤回已批准别名（也可按 --from/--to），并重新核对关联的待批准提案
python -m sca.cli alias revoke --db /tmp/sca.sqlite3 --edge-id 1
# 查看历史扫描结论
python -m sca.cli history --db /tmp/sca.sqlite3 --project shopstack
# 导入新版本后查看逐项继承审阅（items 会列出受影响证据）
python -m sca.cli inheritance show --db /tmp/sca.sqlite3 \
  --project shopstack --project-version 2.1.0
# 继承第 1 项；第 2 项缩小到新版本范围；第 3 项不继承
python -m sca.cli inheritance decide --db /tmp/sca.sqlite3 --plan-id 1 \
  --items 1 --decision inherit --note 'same renamed component'
python -m sca.cli inheritance decide --db /tmp/sca.sqlite3 --plan-id 1 \
  --items 2 --decision narrowed --versions '>=2.0,<3.0'
python -m sca.cli inheritance decide --db /tmp/sca.sqlite3 --plan-id 1 \
  --items 3 --decision skipped --note 'source changed independently'
# 重开并撤回部分决定
python -m sca.cli inheritance reopen --db /tmp/sca.sqlite3 --plan-id 1 --items 1
```

既有数据库在首次打开时会自动迁移：别名表补充版本范围、来源范围、批准依据与未采用原因等列，原有记录、来源路径与历史快照全部保留，既有别名的默认范围为不限（`*`）。

重复导入同一内容不会新增证据：来源内容使用 SHA-256 去重，单条记录还有独立的规范化哈希（包含生态、规范身份、版本与构建路径）。导入同一路径的更新内容时，会作为项目的新来源版本保留；导入不同路径但内容完全相同的副本、以及同一路径的重复导入都不会产生重复证据。组件去重通过规范身份在全库范围内追溯，跨构建路径出现的同一组件只归并身份、不合并证据。

## 测试

```bash
python -m unittest discover -s tests -v
```
