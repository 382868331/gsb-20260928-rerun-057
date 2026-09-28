# 离线软件成分分析原型

该项目先归并离线依赖证据，建立项目版本、规范组件身份、来源清单和构建产物路径之间的关系。漏洞资料、网络查询和许可证策略留到后续阶段。

## 身份规则

- 组件身份由生态系统和规范名称组成，并生成不带版本的 Package URL。
- 只比较显示名称不会合并：`pypi/requests` 与 `npm/requests` 是两个组件。
- Python 名称按 PEP 503 归一化，因此 `PyYAML`、`Py.YAML` 和 `pyyaml` 指向同一组件。
- 清单显式给出的同生态别名会合并身份，例如 Maven group 迁移后的旧坐标。
- 版本冲突在项目内标记为 `conflict`；版本一致的多条证据标记为 `unified`。
- 每条证据保留来源清单、原始 JSON、依赖类型、请求者、scope 和构建产物路径。

## 演示场景

`samples/` 中包含同一个项目 `shopstack 2.0.0` 的四份清单：

- Python `requests` 在两个后端产物中重复出现，版本一致，归并为一个组件但保留两条路径。
- Python `urllib3` 分别为 `2.2.1` 和 `2.3.0`，保留冲突。
- npm `debug` 在不同 `node_modules` 嵌套层级中版本不同，保留冲突和两个路径。
- npm `lodash` 在两个嵌套路径中版本一致，明确展示“合并身份但不合并证据路径”。
- Maven `com.example:platform-core` 与旧坐标 `com.example.legacy:platform-core` 通过别名合并。
- PyPI 与 npm 都有显示名 `requests`，但不会跨生态合并。

## 使用

```bash
python -m sca.cli import --db /tmp/sca.sqlite3 samples/*.json
python -m sca.cli report --db /tmp/sca.sqlite3 --project shopstack
python -m sca.cli report --db /tmp/sca.sqlite3 --project shopstack --json
```

重复导入同一内容不会新增证据：来源内容使用 SHA-256 去重，单条记录还有独立的规范化哈希。导入同一路径的更新内容时，会作为项目的新来源版本保留；导入不同路径但内容完全相同的副本不会产生重复证据。

## 测试

```bash
python -m unittest discover -s tests -v
```

