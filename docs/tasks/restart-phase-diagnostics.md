# Windows 启动器：重启阶段诊断

状态：待评审
基线：`87551b4`；延续 [Windows 启动器](windows-launcher.md)的单次请求和原实例接管约束。

## 先读

- [Windows 启动器](windows-launcher.md)、[开发守则](../11-dev-guide.md)。
- `aureus/cli/debug_restart.py`、`tests/cli/test_debug_restart.py`。

## 目标与验收

- 登录或重启前状态请求因网络、无效 JSON 或非对象响应失败时，指出失败阶段，明确未发出重启请求。监听归属无法确认时仍拒绝提交。
- 重启 POST 的传输错误或不符合协议的回执仅报告结果未确认，不打印异常内容，也不重试。只有返回匹配身份的有效回执才进入接管核验。
- 已接受请求而不能确认接管时，明确区分已接受与尚未核实接管；恢复探测只访问首页，不携带旧会话或密码。

## 子步骤与自检

- [x] 旧版运行 `test_interactive_restart_diagnoses_http_failure_by_stage` 的初始 9 项：全部失败，网络及 JSON 解析异常不能正确归类，非对象响应抛出 `AttributeError`。
- [x] 实现后 `tests/cli/test_debug_restart.py` 29 项全部通过；覆盖三阶段各自的传输与 JSON 失败、回执身份不匹配、确认后的接管等待超时及首页探测先超时后恢复。断言提交前失败不发重启 POST、提交后不重试、接管探测不转发密码或会话、输出不含合成敏感内容。
- [x] 改动 Python 文件 Ruff 规范和格式检查通过，Pyright 0 项错误，架构规则 8 条通过，差异检查通过。未运行全量检查或接触正式实例。

## 不做

不改正式进程、系统数据目录、网络超时、重试策略、启动器归属验证或服务端 API。不得真实登录或发出重启请求。

## 待设计问题

无。
