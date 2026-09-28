# 离线 Python 软件成分分析

本阶段只做离线依赖证据归并，不接入漏洞资料。系统读取项目自带依赖清单，建立项目版本、组件身份、原始清单记录、构建产物出现位置和间接依赖链。

## 输入格式

- `pyproject.toml`：读取 `[project].dependencies`。
- `requirements.txt`：读取直接依赖和精确版本约束。
- `dependency-lock.json`：按 `artifacts[].dependencies[]` 读取不同构建产物，支持 `purl`、`ecosystem`、`type`、`dependency_chain`。

组件身份使用 `(ecosystem, canonical_name)`，PyPI 名称按 PEP 503 规范化大小写、连字符、下划线和点。例如 `Flask`、`flask` 会合并；`pkg:pypi/left-pad` 与 `pkg:npm/left-pad` 即使显示名称相同也不会合并。

同一组件在不同构建产物中出现时，组件身份合并，但 `occurrences.artifact_path`、版本和依赖类型逐条保留。同一组件出现多个已解析版本时，报告标记为 `conflict`，不选择版本，也不丢弃任何证据；所有证据都可回溯到源文件、记录下标和原始 JSON 或 requirement 文本。

## 使用

```bash
offline-sca --db sca-results.db import samples/demo-app/pyproject.toml samples/demo-app/requirements.txt samples/demo-app/dependency-lock.json
offline-sca --db sca-results.db report
offline-sca --db sca-results.db evidence pypi urllib3
```

重新导入同一清单时按文件 SHA-256 去重，不会新增 manifest、occurrence 或版本结论。

## 测试

```bash
python -m pytest
```
