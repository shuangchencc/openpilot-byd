# P4 pandad、逐板屏障与消息链离线验收

结论：P4 已接续 P0/P1/P2/P3 实现，并通过本报告声明范围的离线验收。
本次验收源码提交：`4d374354317a65c0b23895533f23b403cc226518`；分支：`songplus/dos-usb-p4`。
机器结果时间（UTC）：`2026-10-17T19:15:04.328722+00:00`。
没有连接 C3XL、打开实物 Panda、修改设备系统、切槽、复位、刷写固件或发送真实 CAN。

## 交付与接入范围

- `ControlSession`：逐板 health、三路 CAN-health、safety 写入与读回、heartbeat、退出与重新接合屏障。
- `PandadRuntime` / `PandadLoop`：复用 P2 传输与 P3 注册/路由，注入模拟 I/O、输入与发布器，提供有界调度周期。
- `cereal_bridge`：使用官方固定 schema 进行真实序列化/反序列化，保留两槽与原始观测时间。
- 四处消费者的显式 profile 分支：selfdrived、hardwared、card 和 UI；附消息策略、入口防护及回归测试。
- [独立补丁清单](../../patches/openpilot/p4/manifest.json)、[维护与复验说明](../../patches/openpilot/p4/README.md)、[机器结果](result.json)。

Linux 编译、链接实际 pandad 的 `main.cc/pandad.cc/panda_safety.cc/Panda/SPI`、
common/msgq/cereal 与新兼容层，只运行 `--compat-offline` 的有界 fake-I/O 入口。
macOS 构建实际 cereal/runtime 测试入口；官方 SPI 依赖 Linux spidev，未声称 macOS 完整 daemon。
保留原默认 SPI 路径；显式 profile 的普通启动在硬件导入/发现前拒绝，避免旧包装器的自动 reset/flash。

所有 profile 仍是合成身份，safety 仅允许 noOutput；真实 device、TX、firmware 权限为 false。
“接合/发送通过”仅指 fake backend 的模拟调度许可，不代表 noOutput MCU 允许车辆控制。
尚无实物发现、实时 Pub/Sub 适配或部署入口，未启动完整 Python daemons。

## A40–A46 的实现与验收

| 验收 | 已实现并验证的行为 |
| --- | --- |
| A40 health/消息完整性 | 固定两槽；每板精确48字节 health、三路64字节 CAN-health；短读、旧ABI、缺板或过期使整组无效；重发不刷新源年龄 |
| A41 safety 屏障 | 必须显式两份 noOutput 配置；逐槽参数独立，alternativeExperience 使用官方共享字段；offroad 配置、逐板写入和读回一致后才通过 |
| A42 消费者与调度 | 真正 cereal 消息，源时间/接收时间/Event.valid 检查；执行消费者实际函数或分支；输入每轮≤16、控制优先、双板各一次读/发机会 |
| A43 诊断和启动边界 | 不隐式选择 ELM327、发起查询或切 OBD；card 控制副作用前拒绝；Linux 实际可执行文件8种 compatibility 参数组合均提前退出2 |
| A44 外围角色 | ignition 按配置来源做 OR；线束、电源、风扇按显式角色取值；风扇与电源分属两板也正确；必需观测缺失则消息无效，unsupported IR 不写入 |
| A45 heartbeat/退出/恢复 | 故障、offroad、调度超期或旧消息关闭门禁并清队列；健康板执行 noOutput→读回→heartbeat(false)；重连需新handle、offroad重新配置、onroad与新disabled→enabled变化 |
| A46 外围故障 | 缺失/过期fan RPM、deviceState异常或过热关闭模拟控制；不伪造安全风扇输出；不确定发送只尝试一次，不向另一板重放 |

角色快照的电压/电流来自 power_monitor，fan 来自 fan_control；peripheralState 时间采用两者较早的观测。
进入 offroad 立即使旧 control epoch、队列和 engaged heartbeat 失效，保留刚收到的 offroad 观测供显式重新配置。
输入在读取前捕获 generation/control epoch，缓冲消息不能在故障后重新标记成当前会话。

## 本次实际结果

| 范围 | 通过结果 |
| --- | --- |
| Python P0–P4 | 90项：原70项、消费者17项、入口防护3项 |
| P1 CAN 原生回归 | 13组、47031项断言；4096次确定性模糊输入，220项独立对照 |
| P2/P3 原生回归 | macOS、Linux 各111组，包含原SPI wire参考与双板路由回归 |
| P4 控制会话 | 每平台27组、5617项断言 |
| 固定固件源码 liveness oracle | 每平台13组、343项断言 |
| pandad/cereal 集成入口 | 每平台22组、2046项断言 |
| 共享事件循环 | 每平台10组、850项断言 |
| P4 新增原生合计 | 每平台72组、8856项断言；加P2/P3为每平台183组 |
| P3 独立跨语言对照 | 每平台8种fixture、48条路由、96项比较 |
| 新增原生消息→Python消费者 | 每平台11条实际cereal消息、55项检查；固定双槽与0/1/2/4/5/6路由 |
| 失败/跳过 | 上述范围均为0失败、0跳过；C/C++启用ASan/UBSan |

本次在新目录按顺序重放五份补丁，核对211份官方输入和72份结果文件的精确集合与哈希，
并核对前序补丁、manifest、依赖及实际编译所用项目头文件。没有改写旧报告或旧补丁。
Linux 使用本机 Lima 构建 VM，验收在私有 user/network/mount namespace 中执行，隐藏设备节点；
macOS 使用禁网和设备节点沙箱。完整机器证据中分别记录两平台构建、运行与源码哈希。

新增 Linux 依赖在验收前从固定官方 uv.lock 的同版本、同URL、同SHA wheels 准备，
只解包到项目构建目录；未修改 VM 系统包。正式验收不下载、不自动补装依赖。
Python 使用固定3.12.13；固件 oracle 直接提取锁定 C 函数，HAL、safety-hook 及无关驱动调用使用明确 stub；未验证 MCU safety-hook 本体。

## 时限、固件语义与未完成项

health/CAN-health/heartbeat完成及必需fan观测年龄均 <250ms，heartbeat 周期100ms；
selfdriveState/sendcan <100ms，TX排队 <20ms，deviceState <750ms。
外围操作20Hz，pandaStates 10Hz，peripheralState 2Hz；源年龄不因发布刷新。

10ms循环预算是合作式、观测后的界限；达到界限关闭模拟TX并停止后续输入/CAN调度，仍尝试发布失效状态。
两次5ms控制调用即可耗尽预算。它不保证最坏负载持续可用、固定顺序轮询无饥饿，
也不能抢占阻塞内核/回调；这些不是 C3XL 实测实时性能。
调度间隔≥250ms、时间倒退、输入/发布器异常均已离线测试关闭门禁。

锁定固件代码的实测 oracle 明确区分：heartbeat(false) 不立即清除 controls；
第三次1Hz engaged mismatch 清除 controls；heartbeat计数在ignition on/off对应5/2边界进入SILENT。
软件 watchdog 的严格 `>375000μs` 条件记录fault，循环停止时不会自行执行，恢复执行后才观测；
它不等价于即时SILENT或硬件看门狗。计划退出执行三个有界轮次，不声称突然掉电后还能执行。

P5 F4完整BSP/候选固件、P6扩展故障/安全与目标机整合、P7维护封装仍未完成。
实物板型、恢复链、驾驶员监控硬件、宋 PLUS 协议/controller/safety 与车辆验收仍需独立推进。
本结果不构成设备兼容性或可部署结论。
