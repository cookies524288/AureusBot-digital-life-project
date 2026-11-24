# Windows 默认数据目录切换手册

本手册分为两部分：可重复的**合成预演**，和仅供维护者日后执行的**正式操作**。预演不启动 Aureus，不查询正式数据或运行状态，也不调用模型、QQ 或任何网络服务。

## 1. 仅用假数据预演

在含本任务提交的检出根目录执行。将 `$python` 指向已准备好依赖的 Aureus Python 解释器，可以复用相邻主检出的 `.venv`，不用装依赖：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = (Get-Location).Path
$python = (Resolve-Path -LiteralPath '../AureusBot-digital-life-project/.venv/Scripts/python.exe').Path
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('aureus-cutover-fixture-' + [Guid]::NewGuid())
if (Test-Path -LiteralPath $fixture) { throw '预演目录已经存在' }
& $python -m pytest -q tests/test_home_cutover.py tests/cli/test_windows_launcher.py --basetemp $fixture
if ($LASTEXITCODE -ne 0) { throw '预演失败，不进入正式切换' }
Write-Output $fixture
```

每次使用全新的 `--basetemp`：pytest 会清理传入位置中已有内容，**绝不要把它指向任何实际数据目录**。主用例 `test_copy_checks_latest_stopped_home_and_launcher_fallback` 在其中创建 `active home 合成` 与 `fixture project 合成/aureus_home`。用例先持有正式内核所用的同类锁，提交一条仍在 SQLite WAL（预写日志）里的记录，验证拒绝复制；关库且释放锁之后才用 Windows 自带 Robocopy 复制，检查最新记录、完整性、隐藏文件、嵌套媒体、空目录、真实 `_launcher_home()` 选择和回退。其他用例验证不合并、不跟随重解析点，以及同长度损坏可被发现。

## 2. 正式切换前的条件（本预演不执行）

1. 等交接约定的修复正式验收，取得维护者明确许可并安排停机窗口。以**届时运行的当前主目录**为源，不能抄历史路径或用旧快照。维护者先核实当前实例实际数据根、网页端口及 QQ 网关连接状态，记录仅必要的路径和状态；不要导出身份、聊天、配置或密钥内容。
2. 确认正式项目根目录是包含 `Aureus.exe` 和 `pyproject.toml` 的受控检出。目标就是其直接子目录 `aureus_home`。目标不存在时才创建一个空目录；已存在时必须原本就为空且不是重解析点。源、目标、各上级以及源内容不得有联接或符号链接。目标若有任何条目（含隐藏文件或空子目录）、路径重叠或无法核实，一律停下，不覆盖、不清空、不合并。确认目标所在文件系统能保存文件和属性，有足够空间，且访问权限不比原目录宽。若配置或网关另有目录外依赖，先单独确认，不能把整目录校验当作目录外依赖已迁移。
3. 记录原启动方式及 `AUREUS_HOME` 的实际来源（例如启动脚本、当前进程、用户或系统环境）；如有 `aureus-home.txt`，之后改指向前只做同目录备份，不丢弃。不要为切换而一并修改模型、网关或其他配置。
4. 用原来的 Aureus 网页、终端 `/quit` 或托盘「退出」**正常关闭**，等待内核及托盘父进程完全退出、原端口不再监听、该实例 QQ 会话已断开。不是「重启」，不是只关浏览器或关闭终端窗口。暂停外部自动启动流程，并确认备份、维护或其他写入器不会访问两端。直到核验与切换完成，不要运行第二个工具窗口、再次启动旧实例或让别的程序写入任何一端。单实例锁只能防同一源目录上的 Aureus，不替代这些人工确认。

## 3. 停机复制与校验（仅获准后）

从含校验脚本的已审查检出根目录执行。下例假定该检出也有可用 `.venv`；如果没有，`$python` 可用同版本、已准备好依赖的相邻检出解释器，但 `PYTHONPATH` 必须指向**正在运行校验脚本的检出**。路径由维护者填写为已经核实的绝对目录，不要以示例或历史值代替。不要另跑 `aureus export/import`（导出包故意排除密钥和部分文件）或 `robocopy` 镜像、移动、清理选项。

```powershell
$approvedSource = Read-Host '已核实、已正常停机的源数据根目录（绝对路径）'
$approvedProject = Read-Host '已核实的正式项目根目录（绝对路径）'
$target = Join-Path $approvedProject 'aureus_home'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = (Get-Location).Path
$python = (Resolve-Path -LiteralPath './.venv/Scripts/python.exe').Path
& $python -m scripts.check_home_cutover $approvedSource $target --confirm-stopped
if ($LASTEXITCODE -ne 0) { throw '复制或校验未通过；不要切换或清理任何目录' }
```

校验入口用源目录的 `aureus.lock` 持锁运行 Robocopy；锁标记可能保留在源端，目标不复制这一运行文件。核对普通文件的相对位置、目录结构与 SHA-256（普通文件主数据流的内容指纹），只显示结果和文件总数，不解析、不输出内容、文件清单或指纹。它不校验 ACL（访问权限）或盘外依赖。保留源目录；即使 Robocopy 复制后报错，也保留目标现场，不向半成品重跑，不把它选作运行目录。工具不改 Windows 启动入口，不保证整个操作期间无人操作目标；授权前必须落实独占维护窗口。

## 4. 校验成功后切换与验收（仅获准后）

1. **只在复制校验成功后**，退出原托盘后备份并移开正式项目的 `aureus-home.txt`（如原本存在），先确认备份名不占用。让新启动进程不继承 `AUREUS_HOME`：从准备启动的 PowerShell 清除**本进程**变量；若维护者要求日后也能双击启动，另须核对并按授权撤销 Explorer 实际继承的持久覆盖。环境变量有效值优先于指针；指针存在且有效时优先于默认目录。空/无效的指针文件不是默认回退，不能以清空文件替代移开。
2. 从**正式项目根目录**开启一个没有带开发树 `PYTHONPATH` 的启动终端，在其中用实际项目的 `.venv` 检查 `_launcher_home()` 的返回值确实等于正式项目的默认目录。探针只做路径选择，不打开库或启动服务。然后经原启动器从**同一终端环境**正常启动一次，保持正式启动模式和原网关，不加 `--fake`；未通过探针时不要启动。

   ```powershell
   Remove-Item Env:AUREUS_HOME -ErrorAction SilentlyContinue
   Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
   & ./.venv/Scripts/python.exe -c "from pathlib import Path; from aureus.cli.windows_launcher import _launcher_home; assert _launcher_home().resolve() == (Path.cwd() / 'aureus_home').resolve()"
   if ($LASTEXITCODE -ne 0) { throw '启动器没有选择正式默认目录' }
   ```

3. 启动后通过实际默认目录的网页登记、监听端口和托盘归属核对同一正式实例只恢复了一次。下面的探针只查询这两个明确目录各自的本机网页登记：默认目录应有新启动器登记，原目录应无登记；用返回的端口和 PID 对照切换前端口与本次实际启动器。还要核对网页能正常登录并确认既有 QQ 网关已重新连接；不发送任何测试消息，不公开聊天或密钥。仅能打开网页或仅复制成功都不算验收。

   ```powershell
   & ./.venv/Scripts/python.exe -c "import sys; from pathlib import Path; from aureus.web.runner import running_web_instance; print('默认目录:', running_web_instance(Path.cwd() / 'aureus_home')); print('原目录:', running_web_instance(Path(sys.argv[1])))" $approvedSource
   ```

## 5. 回退，不丢数据

- **未尝试启动新目录**：校验或选路测试失败就停；保持或恢复原指针和原环境，确认 `_launcher_home()` 重新指向原目录，才用原启动器恢复一次。保留原目录与任何失败的目标，不清理或覆盖它们。
- **已尝试启动新目录**：即使网页或 QQ 尚未恢复，也按新库可能已写入处理。先正常关闭新实例并确认进程与 QQ 会话都结束；保留并备份完整新目录。原目录只保留切换前的数据，不能把它当成已有切换后写入的目录。由维护者决定修好继续使用最新目录，还是保留两份后恢复旧指针和环境以恢复服务。若恢复旧目录，先说明切换后写入仅留在新目录、不会自动并入；单独处理这些差异，严禁覆盖、自动合并或同时启动两份。
