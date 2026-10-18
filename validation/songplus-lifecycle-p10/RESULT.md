# P10 离线实现与验收结果

结论：**P10 离线工作完成，仍为 BLOCKED_FOR_DEPLOYMENT**。真实 profile 保持
UNBOUND。没有连接 C3XL、发现/打开真实 Panda、修改系统/槽、reset、flash 或 CAN TX。
本轮未重新编译 ARM 固件，未做真实车辆回放/控制验收。

## 冻结版本

- 外层受保护源码提交：`d4e18fe91597c632b07074d27ea13ae1fa901f5a`。
- downstream 分支 `songplus-lifecycle-p10`，结果提交：`8cb3acecc0189d62920144cdc27b7d58ea69f8b4`。
- P10 lock SHA256：`64fe409526045fd23ef5b80fb6b80666af42ce32cc28bb915358d3f793af8611`。
- 官方 openpilot `35e78a9d9518855ec0dacec919e719624a28131f`；Panda `a59195163c32a3a375ea6c86715dc08fd0984c1b`。
- opendbc `b3bd79a659dae961af169a3baac723961f720133`；msgq `0e266c1dbcf7328beee3e57b4a8688555387c877`。
- P0–P9 的补丁、维护锁和官方 gitlinks 未修改。P10为新增独立补丁与维护层。

## 完成的实现

1. **持续发现和重探测**：owner 每 tick 最多一次 start/poll；后台单 worker 做原生
   探测和清理。50 ms 重探测、100 ms 任务预算、250 ms 快照最大年龄、10–500 ms
   有界退避。只发布完整双板、实际逐板时间和新 revision；迟到/旧 generation、旧
   nonce、换板、掉板、过期、时钟回退均撤销整组。有限历史耗尽停止，不自动清空。
2. **原生执行器**：USB 保留原句柄重读 UID/板型/协议/签名/F4能力，不重复 open/claim；
   每次额外扫描 normal-firmware inventory，拒绝多板/少板/未知描述符。混合 USB/F4 +
   SPI/H7 在注入 I/O 下调用 P9 provider 实现与 P10 fence。取消/析构不 join；阻塞 lane 仍占用全进程
   上限，不能重建无穷线程。先撤双板资格，再在 worker 关闭句柄；迟到交接、旧任务
   取消新任务、重复 commit、代理/Clock 晚析构和 fork 继承使用均有回归。
3. **SPI 新鲜性**：同 fd 全 session flock；无效帧排空旧响应，再完成两个不同 ID
   的只读 UID fence，后续 ID 单调递增且不 wrap。不是随机数/磁盘计数器推断。直接
   编译锁定 `panda/board/drivers/spi.h` 的 cache/CS 状态机，替代 DMA/endpoint依赖；
   CRC外设符号隔离避免覆盖zlib。覆盖缓存命中、半读/4096字节响应、进程重启模拟、进程竞争、
   崩溃解锁、CRC/ID/短读/超时/取消。fork child不可复用会话或解开父进程锁。
4. **生产启动门槛**：离线诊断、原生只读、noOutput runtime、车辆控制、部署分层评估；
   证据关联两板、固件、维护链、当前 process epoch/revision 和 live SPI proof。
   没有真实 verifier/生产启动器，所有授权字段始终false。P10真实main在硬件初始化前
   默认返回3；普通serial不能绕过，未知compat参数/profile环境变量拒绝。保留旧SPI回归。
5. **维护封装**：独立维护锁、确定性源码重放、Mac/Linux完整入口、只含源码的Linux CI、
   严格日志/二进制/命令/测试ID校验、固定发布阻断门槛；源码和依赖构建前后哈希校验。

## 本轮新执行的验收

正式记录：`results/p10-lifecycle-full-003/validation/result.json`，时间区间
`2026-10-18T07:07:32.861883+00:00` 至 `2026-10-18T07:14:58.835146+00:00`。

下表为“测试组 / 断言”，全部零失败、零跳过，ASan/UBSan启用：

| P10 新套件 | macOS | Linux |
| --- | ---: | ---: |
| 持续发现/重探测调度 | 38 / 1158 | 38 / 1158 |
| SPI 跨进程新鲜性 | 25 / 2032 | 25 / 2032 |
| 生产启动门槛 | 34 / 975 | 34 / 975 |
| 异步执行器与生命周期 | 25 / 373 | 25 / 373 |
| 原生 USB/SPI 混合链路 | 24 / 2803 | 24 / 2803 |
| 合计 | 146 / 7341 | 146 / 7341 |

- 同一P10源码树新跑 P9：Mac/Linux各102组；新跑P8 runtime：
  Mac 28组、Linux 30组。
  其嵌套P0–P6协议/native/SPI/运行时/loop/故障回归全部通过，证据逐层校验。
- Linux手工编译/链接实际pandad main、P10模块和旧单H7/SPI；运行离线owner、生产gate
  诊断，以及default/serial/env/compat拒绝分支。执行真实SConscript的惰性build contract，
  **不宣称执行完整SCons或目标C3XL启动**。
- Mac源码contract：7命令、56项测试；独立Linux源码contract：7命令、
  56项测试。独立CI来自相同提交的git archive（369个tracked文件），
  无gitlinks源码、.git、local、results或缓存依赖，源码前后hash一致。
- 两份独立重放得到相同的324文件源码包：`a4a508c3755b8bb7dc63a2247675b22c0e56cfcf84f2edb871edfdc390cf1cbe`。
  此项证明源码重放确定性；不宣称不同平台二进制相同或跨机器SDK位级可复现。
- 新编译依赖清单：Mac 886项、Linux
  356项，构建前后未变；这是本次host SDK来源记录。
- 历史P9 full-003完整发布门槛重新校验。它引用的ARM构建、资源/safety和98段车辆
  回放是**历史前置证据复核**，本轮新增ARM构建数/车辆回放段数均为0。

尝试记录：full-001启用整个checkout清洁度检查，因同目录并行的非P10未提交工作而在
编译前拒绝；未运行套件。full-002 的 Mac 全部通过，但 Linux 在新 executor 的两处
misleading-indentation 告警被 -Werror 中止；通过独立修复补丁明确控制流后，full-003
从头执行两平台全部验收，成为本轮唯一正式通过批次。失败日志保留，不计入通过结果。
本地入口逐字核对全部受保护文件与冻结提交；未放宽CI的clean-checkout规则，也未清理并行改动。

## 仍未解决及下一阶段

- 真实板卡身份/MCU修订、精确固件语义、USB active alt0、SPI节点/别名和所有master
  遵守同节点flock、线束/角色/供电、系统与恢复链均需实机证据，不能自动绑定。
- USB serial只在初次保留句柄时读取，周期fresh字段限于UID/板型/协议/签名/F4能力；
  不虚报重新读取serial。native owner端普通传输仍同步，后台化不证明kernel最大时延。
  卡住lane、退出、断连、资源峰值和真实时限仍须台架验收。
- 真实生产verifier、noOutput运行授权和生产launcher仍待后续实机阶段接入；当前只读
  proxy不会写heartbeat/safety配置。驾驶员相机缺失、声音告警、完整模型/相机/资源预算、
  宋PLUS控制器及独立safety/封闭场地验证仍未完成。
- 下一步优先按 `lifecycle_maintenance/HARDWARE_GATES.md` 做被动实机身份/恢复/占用核实，
  然后在明确台架操作范围下验证真实访问者和内核/断连行为，满足证据后再谈真实profile。

勘误：历史P9文档的10ms快照寿命表述有误，实际 `kLoopMaxGapUs=250000`（250ms）；
本轮未改变该常量，以新增说明纠正，保留历史锁/文档。
