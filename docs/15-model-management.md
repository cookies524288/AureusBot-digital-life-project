# 15 · 模型管理

> 2026-11-16 新增（D30）。设计参考 MaiBot 的模型配置（`src/config/model_configs.py`，GPL-3.0，只参考结构，不复制代码）。

---

## 1. 目标

- 用户可以自由选择模型和服务商，项目里不写死任何具体模型名；
- 服务商、密钥、模型、价格统一存放在一处，换模型只改配置，不改代码；
- 支持 D24 规定的四种协议：OpenAI、OpenAI Responses、Anthropic、Gemini（底层用 litellm）。

## 2. 三层结构

| 层 | 管什么 | 主要字段 |
|---|---|---|
| 服务商 `providers` | 去哪里调用、怎么认证 | `name`、`base_url`、`protocol`、`api_key_env`、`timeout_seconds`、`max_retry` |
| 模型 `models` | 服务商提供的某个具体模型 | `name`（自己起的名字）、`identifier`（服务商给的模型标识）、`provider`、`kind`（`llm` / `embedding` / `vision`）、`price_in`、`price_out`、`price_cache_in`（元 / 百万 token，可选，只用来显示估算费用，D37）、`temperature`、`max_tokens`、`reasoning_levels`（该模型支持的思考等级） |
| 器官 `organs` | 每个器官用哪些模型 | 器官名（`planner`、`replyer`、`judge`、`summarizer`、`vision`……）、`models`（按名字引用，有顺序）、`strategy`（`sequential` / `balance` / `random`）、`max_tokens`、`temperature`、`reasoning_effort`（可选的思考等级） |

换模型：只改 `organs` 里引用的名字。加新服务商：在 `providers` 加一项，并用 `aureus keys set` 录入密钥（D36）。

思考等级来自 litellm 1.109.1 支持的 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`、`default`。模型用 `reasoning_levels` 声明支持项；器官的 `reasoning_effort` 必须被它引用的每个模型支持。未设置时不向 litellm 传这个参数。

## 3. 存放位置

- `<数据根目录>/config/models.toml`：服务商、模型、器官，不含密钥，可以放心导出和分享。模板是仓库里的 `config/models.example.toml`（步骤 0.5 创建）。
- `<数据根目录>/secrets.vault`：加密的密钥库；解开它的主密钥放在同目录的 `secrets.key`（D36，见第 7 节）。`providers` 里只写 `api_key_env = "AUREUS_KEY_某某"`，也就是密钥的名字，不写密钥本身。同一个名字也可以从环境变量或 `.env` 取到，读取顺序是环境变量 > `secrets.vault` > `.env`。
- 与 MaiBot 的区别：MaiBot 把密钥直接写在配置文件里。我们改成只引用密钥的名字，密钥本身放在加密的密钥库里（D36），这样导出配置不会带出密钥。

## 4. 校验与安全

- 器官引用了不存在的模型、模型引用了不存在的服务商：启动时报中文错误，指出文件和字段。
- 价格可选（D37）：账本和预算按 token 计，不靠价格运作；填了价格只用来在账本里显示估算费用，不参与限额判断。
- 某个服务商的密钥缺失：该服务商下的模型标记为不可用，调用前就拒绝，不发出空请求。器官的列表里还有可用模型时顺延，一个都没有则报错。
- 日志和报错里不出现密钥（沿用 `common` 的密钥过滤）。这层过滤只挡「启动时已知的密钥原文」，URL 编码、截断或拆开拼接过的挡不住，所以代码不能主动把密钥写进日志，过滤只是兜底。

## 5. 以后的扩展

- 预算档位（Phase 1）：器官的模型列表按「从贵到便宜」排列，精力低时改用靠后的模型，不需要新机制。已在 1.6 实现：正常档从第一个模型开始，节省档从第二个，低电量和透支保护从最后一个，不可用时先往更便宜的试、再往更贵的试（D59）。
- 配置热重载、服务商预设模板、从服务商接口拉取模型列表：用得上的时候再做（开发守则 2.5）。
- 导入 MaiBot 的模型配置：由 `tools/migrate/` 提供转换命令，用户自己运行。密钥由用户用 `aureus keys set` 自己录入，转换工具不搬运密钥。
- MaiBot 控制台的公开模板源码里带有第三方预置的密钥，我们既不复制也不使用。

## 6. 落地

步骤 0.7（2026-11-16）已实现，位置都在 `aureus/organs/`：

- `model_config.py`：三层配置的读取和校验，引用错误在启动时报中文错误；
- `api_keys.py`：按环境变量名取密钥，环境变量优先于 `.env`；
- `llm_usage.py`：从 litellm 的返回里取 token 用量，按 `models.toml` 里的价格自己算费用，不用 litellm 自带的价格表；
- `llm_adapter.py`：真实器官 `LLMOrgan`，按器官里的模型顺序尝试，失败顺延，全部失败抛 `OrganCallError`；
- 仓库里新增 `config/models.example.toml`，`.env.example` 已改成 `AUREUS_KEY_` 开头的变量名。

测试全部用假的调用函数，不联网、不花钱；真实调用一次需要用户填好密钥并同意。预算上限和账本在步骤 0.8，不在这里。

---

## 7. 密钥加密存放（D36）

> 2026-11-16 新增，同日修订：口令方案改成密钥文件（环境变量不随数据目录走，可移植性差）。取代第 3 节里「密钥只放 `.env`」的做法：`.env` 降为最低优先级的回退。

### 7.1 保护什么，不保护什么

- 保护：密钥不再明文躺在 `.env` 里。`secrets.vault` 是密文，连密钥的名字都看不出来，单独泄露这个文件（贴出去、截图、误传）读不出密钥。
- 不保护：整个数据根目录被拿走。`secrets.key` 和 `secrets.vault` 在同一个目录，拿到整个目录就能解开。先这样实现，以后再加强（7.9）。
- 两个文件都不进 git，也不进导出包（7.7）。

### 7.2 两个文件

- `<数据根目录>/secrets.key`：主密钥（master key，也叫「入口密码」），第一次录入密钥时程序随机生成，用户不用记。内容是 JSON：`format_version`、`protection`（现在只有 `none`，给 7.9 留位置）、`key`（Fernet 格式的随机密钥）。
- `<数据根目录>/secrets.vault`：加密的密钥库，JSON 外壳（`format_version` 加 Fernet 令牌），令牌里面才是 `{密钥名: 密钥}`。
- 加密用 cryptography 库的 Fernet（带完整性校验），不自己拼算法。主密钥不对或文件被改动，都解密失败，不会读出错的内容。
- 写入先写临时文件再替换，中途崩溃不留半个文件。`secrets.key` 在类 Unix 系统上设成 0600；Windows 不另外收紧，沿用数据根目录自带的访问控制（D52：数据根目录不要放在别的账户能读的共享位置）。
- 改密钥库（`set`、`remove`）前先拿同目录下 `secrets.vault.lock` 的文件锁，两个 `keys` 命令同时运行不会丢掉一条；等 10 秒还拿不到就报错，不跳过锁硬写。只读不加锁（D52）。
- 有 vault 没有 key（或内容不对）：拒绝读取并说明原因，不自动重新生成，也不回退到 `.env`；找回文件还是删掉重录，由用户决定。有 key 没有 vault：当作还没有密钥库。`format_version` 比软件新：拒绝读取（同 D28）。
- 主密钥丢了无法找回，没有后门；密钥可以去服务商重新申请。

### 7.3 启动时怎么解锁

启动时自动读 `secrets.key` 解开 `secrets.vault`，不需要任何人输入，没人在场也能启动、崩溃后能自动重启。只在启动时做一次，解开的密钥只在内存里，用 `SecretStr` 保存，不进心跳循环（见 D41 第 3 条：心跳不能被慢操作卡住）。

### 7.4 读取顺序与代码结构

- 顺序：环境变量 > `secrets.vault` > `.env`。环境变量留给临时覆盖和测试，只放单个密钥，不再负责「解锁」，所以没有可移植性问题。
- 加解密和文件格式放 `aureus/common/vault.py`，只管文件，不认识器官；来源链放 `aureus/organs/api_keys.py`。
- 新增 `KeySource` 接口（`get(name)`、`names()`），三个来源：环境变量、密钥库、`.env`。`KeyResolver` 按顺序问，取第一个非空值；对外的 `get`、`secret_values` 和原来的构造写法不变，适配器、`models.toml` 和现有测试都不用改。
- 密钥库存在但打不开：报错停止，不回退到 `.env`，否则主密钥丢了会悄悄用上旧明文。不存在：跳过这个来源。
- 启动时 `.env` 里还有 `AUREUS_KEY_` 开头的密钥：打警告（只写变量名），建议迁移。

### 7.5 命令 `keys`

0.7a 先用标准库 argparse 做一个只有 `keys` 子命令的最小入口：`uv run python -m aureus keys 子命令`（不然 0.10 之前没法录入密钥），0.10 再并进完整命令行。密钥通过提示输入（不回显），不经命令行参数，不进 shell 历史。

- `set 名字`：录入一条；还没有密钥库时自动创建主密钥和密钥库。
- `list`：只列名字，不列值。
- `remove 名字`：删掉一条。
- `migrate`：把 `.env` 里 `AUREUS_KEY_` 开头的密钥搬进密钥库；同名已存在时不覆盖，重复运行结果相同；不打印密钥；**不删除 `.env`**，删不删由用户决定（删除数据前要先确认）；不需要输入口令。

### 7.6 日志过滤

补上此前的缺口：日志过滤目前只接了 `Secrets.llm_api_key`，`AUREUS_KEY_*` 没接，所以 `AUREUS_KEY_STAIN` 现在不会被日志隐藏。新规则：交给日志模块的是所有来源的全部密钥（密钥库每一条，加上 `.env` 和环境变量里 `AUREUS_KEY_` 开头的）和主密钥本身，不管 `models.toml` 有没有引用。启动顺序：先解锁，再设置日志；解锁过程不写日志。（D52 起 `Secrets.llm_api_key` 和整个 `Secrets` 类已删除，日志过滤的密钥全部来自 `KeyResolver.all_secret_values()`。）

### 7.7 导出导入与换电脑

- 导出永远不含 `secrets.vault`、`secrets.key` 和 `.env`（取代 [14-user-data.md](14-user-data.md) 第 4 节里「用户可以选择包含 `.env`」的说法），导入后用 `keys set` 或 `keys migrate` 重新录入。
- 想原样搬家：手动复制整个数据根目录，两个文件一起走，到新电脑上不用任何设置就能解开，因为解锁需要的东西全在目录里。

### 7.8 测试要覆盖的边界

主密钥文件缺失、内容不对、为空、两端有空白换行；密钥库被改动一个字节；`format_version` 太新；密钥库为空；有 key 没有 vault；写入中途失败后原文件完好；`migrate` 重复运行；密钥值两端有空白；日志里查不到任何密钥和主密钥（含异常追踪）；导出不含这三个文件；类 Unix 下 `secrets.key` 权限是 0600。全部用编造的假密钥。

### 7.9 以后加强：口令保护（现在不做，位置已留好）

`secrets.key` 里的 `protection` 以后可以加一个 `passphrase`：主密钥改成用口令（scrypt 派生密钥）加密后存放，启动时输入口令，没人在场时才读环境变量。密钥库不用重新加密，只重新包装主密钥，也就是「只加密入口密码」。系统凭据库（keyring）、硬件密钥、密钥轮换、远程密钥管理同样暂不做。

### 7.10 新依赖

`cryptography`。装之前按 [11-dev-guide.md](11-dev-guide.md) 1.5 节核实包名拼写。现在只用到 Fernet，scrypt 等做口令保护时再用。

### 7.11 落地（步骤 0.7a，2026-11-16）

- `aureus/common/vault.py`：主密钥文件和加密密钥库（`SecretVault`），写入先写临时文件再替换；`AureusPaths` 新增 `vault_file`、`master_key_file`。
- `aureus/organs/api_keys.py`：`KeySource` 接口，`KeyResolver` 按环境变量 > 密钥库 > `.env` 取密钥；密钥库启动时解开一次放在内存里，打不开就报错、不回退到 `.env`；`all_secret_values()` 收集全部来源的密钥和主密钥，交给日志过滤；`warn_about_plaintext_keys()` 在 `.env` 里还有明文密钥时只写名字打警告。
- `aureus/cli/` 和 `aureus/__main__.py`：最小的 `keys` 命令，运行方式是 `uv run python -m aureus --home 数据根目录 keys set|list|remove|migrate`；`--home` 可以写相对路径，不给就用环境变量 `AUREUS_HOME`。
- 价格可选（D37）：`price_in`、`price_out` 不填也能通过校验；没写价格或服务商没返回用量时，费用是 None（未知），不是 0；`wasted_cost` 同理。
- 测试覆盖了 7.8 里的绝大部分边界。两项还没覆盖：「导出不含这三个文件」要等导出命令做出来再补；「`secrets.key` 权限是 0600」只在类 Unix 系统上运行，Windows 上跳过。
- 密钥库的解锁、日志设置、明文警告这几步还没有接进启动流程，要等 0.10 做核心启动时按 7.6 的顺序接上（先解锁，再设置日志，再打警告）。
