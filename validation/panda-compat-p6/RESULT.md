# P6 离线验收：F4 能力、双板复合故障与 safety

状态：**PASS_OFFLINE_HOST_ONLY**。冻结设计的 **A60–A65 全部通过**。
逐项证据见 [acceptance.json](acceptance.json)，完整本轮命令、源码/依赖/二进制哈希及计数见
[result.json](result.json)。这不是实物、完整 AGNOS、宋 PLUS 车辆或部署验收。

## 交付与版本

- 官方 openpilot：`35e78a9d9518855ec0dacec919e719624a28131f`。
- 官方 Panda / opendbc：`a59195163c32a3a375ea6c86715dc08fd0984c1b` /
  `b3bd79a659dae961af169a3baac723961f720133`。
- P0–P4 host 补丁后新增 P6：`d4d610ecb08ab27200e48c07757b147861f710a5`，
  分支 `songplus/dos-usb-p6`。
- 独立 P5 Panda 候选仍为 `aa02e42356f12b5ed4f0dbf553ffc2e8a7211b65`，本轮未改固件。

补丁与复验命令见 [P6维护说明](../../patches/openpilot/p6/README.md)。本轮由官方 Git 归档
重新构建干净源码，应用原 P0–P4、P6 host 及独立 P5 Panda 补丁，核对完整文件集合，
并在运行后再次核对源文件和验证工具哈希。旧阶段补丁/报告及官方 gitlink 保持不变。
正式日志目录：`results/panda-compat-p6/run-5ai0mdeu`，该大目录被 Git 忽略；本页和机器摘要已版本化。

## 实现行为

1. F4 每次连接/组 generation 变化先读取精确 16 字节 F4C1 v1 能力。未核实前阻止
   两板配置/heartbeat 写入；缺失、短读、未知版本/位、过期或 token 不符均拒绝，不能回退旧 ABI。
   H7 保留固定官方 ABI，不发 F4 专属 0xd5。
2. F4 电流、声音和 SPI 错误计数明确不可用；有效电压/温度还需新鲜 health。
   未改 cereal schema：不支持字段为数值占位，消费者必须同时依赖显式 profile 和有效 Event，
   不能把 F4 电流占位当成测量。风扇/电压仍可有效；H7 电流及原字段保留。
3. 掉板、替板、重连、旧 epoch、过期/分片消息、TX/RX 溢出关闭发送并清除旧队列。
   MCU 拒绝回执保存原 slot/local bus/epoch/source 证据，立即关闭两板后续 TX；
   不重试、不换板、不改 safety mode、不走诊断通道绕过。
4. 正常退出先关闭 TX，再至多五轮、十次各 5 ms 配合式操作，随后取消并锁闭。
   模拟 SIGINT 交给 owner loop；另测进程死亡/USB 断电，未假设突然断电后 finally 能执行。
   重连须重新配置并出现新的 disabled→enabled 边沿。

首轮 Linux 严格编译因故障测试的三处单行循环缩进歧义触发 `-Werror`。已增加明确代码块，
保留严格设置；下表来自修正后补丁的全新干净重放。失败运行 `run-wbk_xyhw` 保留，未计为通过。

## 本轮执行结果

| 检查 | 结果 |
|---|---|
| Python profile、协议、消息与实际消费者分支 | 94 项通过 |
| C++ CAN codec / 独立跨语言 oracle | 13 组、47,031 断言；固定 ABI/线格式对照通过 |
| macOS 原生 host、实际 cereal/runtime/loop | 222 组通过 |
| Linux 原生 host、实际 pandad 离线入口/loop | 222 组通过；原 Panda/SPI 编译链接；8 类入口误用全部拒绝 |
| 上述两平台内的 P6 新能力检查 | 各 12 组、1,003 断言 |
| 上述两平台内的 P6 复合故障 | 各 22 组、3,138 断言 |
| 上述两平台内的独立单 H7/SPI | 各 5 组、146 断言；无双板 profile，classic/64B FD 双向正向对照 |
| 原 SPI 线协议黄金向量 | 两平台各 5 条与原实现一致 |
| 当前实际 safety/候选 C，F4/H7 × debug/release | 两平台各 64 组、5,086 断言；各 30 项 F4/H7 经典决策/状态对照一致 |
| 精选官方 opendbc safety | macOS 重新编译 debug/release，49 项通过 |
| P5 实际能力响应与健康解码 | 重新执行 12 项通过 |
| 原生 cereal → Python 消费者 | 两平台各 11 条实际序列化消息；角色、缺失字段与过期状态对照通过 |

上述实际执行测试 **零失败、零错误、零跳过**；原生 C/C++ 使用 ASan/UBSan。
222 组包含旧阶段原生回归及 13 组旧固件 liveness oracle，表中“上述两平台内”的行是其子集，
不能重复相加。64 组 P6 safety oracle 为另列测试；49 项为精选套件，未声称所有车型全量回归。
被排除的 4 项官方 BYD 不适用方法与 1 项空方法保留在机器结果，未计为通过。

P6 safety oracle 执行实际 current opendbc hooks、P5 can_common/can_comms 及逐字抽取的
set_safety_mode/tick/watchdog，HAL 为 mock。F4/H7 是本机编译策略标签；本轮不重新构建 ARM
固件，P5 四个 ARM 候选编译/资源预算属于原报告。P4 旧 liveness oracle 使用 stub hooks，
与本轮执行真实 safety hooks 的 P6 oracle 分开标记。两平台 P6 cases/traces 完全一致。

当前 debug BYD35 是官方 Atto3 参考；旧 enum32 不支持并回到 SILENT。
当前 debug hooks 接受但忽略 param33 这一事实有专门测试，不代表私有旧宋 PLUS 语义被保留；
release 不提供 debug-only BYD35。本工程的实际配置仍只允许 noOutput。

## 仍未覆盖的条件

- 全程未连接 C3XL/Panda、未复位、切槽、刷固件、修改系统或发送真实 CAN。
  macOS 验收禁网/设备节点/IOKit；Linux 在本机隔离 VM namespace 内只用 fake I/O。
- 五轮退出的 50 ms 是配合式传输预算，非内核或硬件实时保证。整组解析隔离会退休两板旧 token，
  因而不会继续通过那些连接发送显式 noOutput；该路径依赖仍运行的 MCU heartbeat 超时。
  正常退出/单板传输故障/MCU 拒绝路径才验证可达兄弟板的独立显式退出。
- 突然进程死亡且 MCU 仍运行时可验证 heartbeat 状态转换；停止/断电的 MCU 不能执行软件 watchdog。
  未验证电气输出、真实供电掉电顺序、物理 watchdog、USB/SPI 时延或并发内核阻塞上界。
- F413/DOS 仍为公开参考板型；实物 profile、板卡引脚、bootstub 信任/恢复链仍未确认。
  P5 最坏中断嵌套栈及继承的 H7 cookie/mic_tx_buf 重叠运行影响仍未解决。
- 真实发现/实时 PubSub、AGNOS/相机/推理、驾驶员监控、完整音频告警、宋 PLUS controller/safety
  与车辆验收未完成。下一阶段为 P7 维护封装与发布门槛；本结果不产生安装/刷写许可。
