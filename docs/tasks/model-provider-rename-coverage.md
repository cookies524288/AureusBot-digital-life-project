# 路线图 3.13 补充：密钥改名与模型引用的网页验收

状态：完成（已合入本地 `main`，仅修改测试与文档）

复审：主审检查 `a3eac18` 的差异并独立复跑 `test_rename_retains_model_reference_and_delete_immediately_removes_it`，1 项通过。缺少来源仍返回 403；改用网页实际发送的同源请求后，改名、引用保留、删除及配置文件不变的既有断言均执行通过。没有修改生产代码，也没有据此将此前的全量检查标为通过。
设计：D77、D84、D87；沿用现有供应商配置同源检查。

## 先读

[3.13 专项](3.13.md)、[网页交互维护](resource-page-ux.md)、[开发守则](../11-dev-guide.md)、`aureus/web/config_api.py`、`aureus/web/static/index.html`、`tests/web/test_vault_api.py` 和 `tests/web/test_models_api.py`。

## 目标与边界

冻结全检 `0dabb40` 的 `test_rename_retains_model_reference_and_delete_immediately_removes_it` 在创建供应商时收到 403，尚未执行改名和删除断言。在最新主线复现，再以实际网页使用的同源模型配置请求建立合成引用。保留请求来源缺失时返回 403 的断言，不把旧式密码字段作为通过供应商保存的条件。

保留原验收：密钥改名保留稳定引用、模型文件字节、解析值和可用状态；删除后解析立即不可用，模型保存的引用仍在。若同源请求后仍失败，先记录实际失败点及可复现证据，再只修覆盖到的产品缺陷。

## 子步骤

1. 独立工作树复现旧测试，核对网页请求和接口契约。
2. 对齐旧测试请求夹具，复跑 Vault 与相邻模型配置专项；记录结果和交接并提交复审。

## 自检清单

- [x] 最新 `main@f6132aa` 上复跑原用例，1 项失败：不带同源 `Origin` 的供应商保存为 `403`，旧夹具预期 `200`。
- [x] `test_rename_retains_model_reference_and_delete_immediately_removes_it` 改用与网页一致的同源保存且不提交密码，仍验证来源缺失为 `403`；原有改名、引用、即时失效和文件字节断言全部执行并通过。未发现需要修改生产代码的缺陷。
- [x] `tests/web/test_vault_api.py` 与 `tests/web/test_models_api.py` 合计 66 项通过；改动测试文件的 Ruff 规范/格式、Pyright（0 错误）、`git diff --check` 通过。
- [x] 只改合成 HTTP 测试和文档，未放宽生产检查、访问正式配置、数据或密钥、调用真实模型、跑全量、合入主线、重启或推送。

## 不做

不改变供应商保存、密钥管理或模型引用语义。不处理其他冻结全检失败。

## 待设计问题

无。
