# P0/P1 独立补丁

已实现独立的离线契约与编解码模块，并通过对应主机验收。它们还没有接入真实 pandad、USB/SPI 后端或 MCU 固件。[正式结果](../../../validation/panda-compat-p0p1/RESULT.md)单独记录，之前的设计快照保持冻结。

| 补丁 | 内容 | 本地源码提交 |
| --- | --- | --- |
| `0001-offline-profile-registry-mock.patch` | 未绑定profile、严格身份/角色注册、模拟传输及测试 | `b01f37b2fa470b62568e1e67455ba645a71e5c36` |
| `0002-pinned-protocol-codecs-tests.patch` | C++ CAN、Python版本/health、C头文件探针、跨语言及组合验收 | `3ab5233f9de0e1c0e65bfb2ca8339077da83c939` |

基线是 openpilot `35e78a9d9518855ec0dacec919e719624a28131f`，配套 panda `a59195163c32a3a375ea6c86715dc08fd0984c1b`、opendbc `b3bd79a659dae961af169a3baac723961f720133`。完整提交、补丁SHA、17份结果文件SHA和编译引用的官方头文件SHA见`manifest.json`；验收与测试证据的对应关系见`acceptance-map.json`。

两个补丁只新增`openpilot/selfdrive/pandad/compatibility/`。开发分支为`songplus/dos-usb-p0p1`，位于本工程忽略的`local/worktrees/openpilot-p0p1`；复验不依赖这个工作树，使用已版本化补丁从固定基线重新生成隔离目录。原始上游checkout及子模块指针没有改变。

在本工程根目录运行：

```sh
python3 tools/validate_panda_compat.py
```

入口要求已安装的锁定Python 3.12.13和支持ASan/UBSan的C++17编译器，不自动下载。默认Python为`upstream-openpilot/.venv/bin/python`，可用`--python /absolute/path/to/python`指定同版本解释器。脚本验证补丁/输入哈希、重放补丁、执行独立测试并写入忽略的`results/panda-compat-p0p1/`。macOS执行阶段禁止网络和设备节点访问，允许编译器/Python所需的空设备及随机源；Linux若使用此入口，报告不会声称启用了macOS沙箱。

维护时从锁定官方提交建立独立分支，按编号依次使用`git am`应用两份补丁，在该分支修改、测试后导出新的`git format-patch --full-index`序列，并更新manifest与验收报告。不要直接修改本工程原始上游checkout、替换gitlink或自动跟随最新master。提交哈希/补丁哈希变化后，旧验收不能继续代表新代码。

模块约束：

- 新的`profiles/unbound.json`采用严格离线schema，所有权限为false。历史设计模板不是runtime输入。只允许`fixture:`合成身份，DOS/F4和RED_PANDA/H7配对是测试目录，不代表本机实物已核实。
- 注册按身份原子完成，固定两块required槽位；未知/重复/缺失/替换板和能力矛盾拒绝。注册成功也不会授予设备或CAN权限。
- 模拟传输只记录内存操作意图，无设备打开、时钟等待、自动重试或reset。脚本容量/超时是fixture边界，不是实测USB实时预算。
- 当前版本严格匹配8字节结构hash，SP098 16/4/5只接受显式离线解析，DP health17拒绝；旧safety 32不映射成当前35。
- CAN长度/FD/flags/地址先校验再编码；无效流输入清空本次输出并使该解析器保持错误状态。`clear()`仅清本地内存。
- health的原始值与单位/能力归一化分开；缺失传感器不伪造健康零值。上游合法的BRS/FD瞬态保留原值并标记不一致；不将协议匹配等同于控制就绪。

现有pandad的父包初始化和自动刷写wrapper没有被导入或替换。测试以独立`compatibility`包加载；实际传输、会话、双板消息/safety门禁和外围接入仍属于P2–P4，F4固件工程与系统级验收属于P5–P7。本目录没有设备安装命令。
