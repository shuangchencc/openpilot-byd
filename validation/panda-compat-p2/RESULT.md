# P2 USB/SPI 传输实现与离线验收

结论：P2 已落地为独立补丁，并通过下述限定范围的离线验收。
本次测试补丁提交：`40d436c8e781b1c2779dd945b73f77225216512e`。
原 SPI 运行路径保持不变。没有连接 C3XL、枚举或打开真实 Panda，
没有修改设备系统、切槽、重置、刷固件或发送真实 CAN。

## 交付与基线

- 官方 openpilot：`35e78a9d9518855ec0dacec919e719624a28131f`。
- Panda：`a59195163c32a3a375ea6c86715dc08fd0984c1b`；opendbc：`b3bd79a659dae961af169a3baac723961f720133`。
- 在原 P0/P1 两份补丁后追加 P2；提交和源码/补丁哈希见 [manifest](../../patches/openpilot/p2/manifest.json)。
- 开发分支 `songplus/dos-usb-p2`。完整重放命令和依赖准备条件见 [维护说明](../../patches/openpilot/p2/README.md)。
- 机器结果、平台标记、编译命令和检查证据见 [result.json](result.json)。

新增 `Transport`、USB 后端/真实 libusb API、SPI 后端/Linux spidev API，以及独立测试。
USB 默认不打开，绑定必须精确，拒绝歧义身份、bootstub/DFU 和错误接口/端点。
两块板的锁、状态、解析缓冲独立；一块阻塞或断开时另一块仍可推进。
短读和部分完成保留实际长度；多分片后失败保留已确认前缀并标注未知剩余量。
仅明确零字节的原始读超时可有限重试；写入不重放，未知读消费也关闭该会话。
晚到的底层断开/STALL/错误不会被截止时间覆盖后误判为可重试超时。

## 本次实际验收

| 范围 | 实际结果 |
| --- | --- |
| macOS arm64，Apple Clang 21 | 59组通过：传输19、USB19、SPI16、时限策略4、生产后端链接/无副作用入口1 |
| Linux arm64，Debian g++12.2 | 同样59组通过；实际链接 libusb 与 Linux spidev 实现 |
| SPI独立参考 | 每个平台5组报文一致；参考采用锁定原始结构及zlib CRC，未构造原Panda对象 |
| P0/P1组合回归 | 70个Python测试，13组C++测试/47031项断言；4096次确定性模糊输入；220项独立跨语言对照 |
| 失败/跳过 | 上述测试均为0失败、0跳过；全部C++检查开启ASan/UBSan |

所有验证都从干净官方源码重新应用 P0/P1/P2，而非直接使用开发工作目录。
macOS 执行环境禁止网络和设备节点；Linux 采用私有 user/network/mount
namespace，设备目录只保留 null/random。输入 tar、依赖和全部补丁文件核验哈希。
本机 Linux VM 只承担构建；libusb 在准备阶段解包到任务目录，未安装到系统。

## 验收项与范围

| 项目 | 已证明的内容 | 保留的边界 |
| --- | --- | --- |
| A20 返回值 | 成功、短读、超时、STALL、断开、取消、部分写；USB分片进入P1解析器；无自动写重放 | 全部传输由注入的模拟API执行 |
| A21 双板隔离 | A阻塞/B继续、A断开/B完成；同板竞争有界，缓冲不混用 | 完整组失效和路由在P3/P4 |
| A22 SPI回归 | 原spi.cc/panda_comms.h/panda.cc/pandad.cc/SConscript不变；5组成功wire一致 | 新适配器有意取消原无限重试，不宣称错误路径完全相同；无USB兜底 |
| A23 构建 | macOS可移植目标；Linux完整USB/SPI传输目标编译链接 | 尚未集成/构建完整活动pandad；不是目标AGNOS/MCU验收 |
| A24 时限 | 请求1–100ms、调用片≤5ms、等锁≤10ms、无待发队列；阈值±1、取消、时钟异常；health/heartbeat纯策略边界 | 真正health采集、heartbeat调度、整组发送屏障留给P3/P4；不保证内核硬实时 |

health轮询/heartbeat周期冻结为100ms，候选最大年龄250ms；来源是锁定pandad
100Hz循环/每10帧调度及明确的主机余量选择，不是测得的C3XL能力或MCU SILENT时限。
完整数值和理由保存在补丁内 `TIMING-BUDGET.md`，由 `timing_budget.h` 纯策略测试核验。

## 尚未完成及下一步

USB `open/claim` 与同步 spidev `ioctl` 可能在内核内阻塞；当前取消是边界间协作检查，
离线模拟不能证明真实设备上的最坏阻塞时间。生产入口只做链接与未打开对象测试。

锁定 Panda 固件不实现 GET_INTERFACE；alt0 描述符不能证明当前处于alt0。
实际 USB 激活需另行核实，默认绑定要求 `bulk_alt0_verified=false` 并拒绝打开；
本实现不通过 SET_INTERFACE、复位或重新配置去补足这一条件。

下一步为 P3 的固定槽位、连接epoch、双向路由、过期队列处理及完整组可用性，
再由 P4 接入消息、health/heartbeat、safety与外围角色。P0的未绑定profile仍拒绝设备权限，
没有把这个底层库自动接进manager/pandad。F4固件、精确板卡身份、恢复链、驾驶员监控
硬件缺口和宋PLUS车辆兼容性仍未由本次验收解决；本结果不代表可部署或可上车。
