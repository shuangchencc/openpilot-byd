# F4/DOS 与 H7 的固件设计

2026-10-18。**本轮只完成本地静态设计；没有实现、编译、连接设备或刷写。** 下面的测试均为待实现验收，不是已通过结果。固定基线为 panda `a59195163c32a3a375ea6c86715dc08fd0984c1b`、opendbc `b3bd79a659dae961af169a3baac723961f720133`，属于已锁定 openpilot `35e78a9d9518855ec0dacec919e719624a28131f`。

正式目标是 **F4/DOS 与 H7 共用新版协议和当前 opendbc safety**，仅分开 MCU/BSP、CAN 控制器和资源配置。旧协议桥可用于离线 fixture、历史数据解码或隔离观察；把旧包改成新版布局不能证明旧 safety 可信，不能因此允许控制。

用户无法提供照片或厂家资料不妨碍离线设计、mock 和候选工程实现。未知 MCU 型号/容量、GPIO、时钟及 bootstub 接受条件留作后续实机门槛，不写成已知值。用户明确的无驾驶员相机、无电话卡功能配置保持为产品约束。现有 bootstub 保留，未来只生成受控的 application 候选；自动刷写和恢复必须与运行/构建路径隔离。

## 源码基准和证据边界

旧 SP098 Python 文件是本地历史快照，能证明宿主库当时期待的协议和 MCU 配置，不能证明 MCU 内的 C 实现。`DEV-e26a80db-DEBUG` 是版本标签，不冒充可复现的 Git commit。现存 `op_rootdir` 没有完整旧 F4/DOS 板级 C 源。DP `94ad85cfda64773a960d048dc11cac02d4aa86a5` 提供 DOS 板头、F4 检测头及引用 `base_project_f4` 的构建片段，**不是完整 BSP/startup/linker/bxCAN 工程**。补齐完整且许可证可追溯的 F4 源码是实现依赖；不能直接把三个片段称为可构建移植。

DP 的 Lite-as-DOS 注释是公开参考代码中的作者陈述，不能替代本机测量。其四个 transceiver 控制包含 CAN2 普通/OBD 复用，不等于四个独立 CAN 控制器。`CAN4` 在旧协议名称中指版本号 4；目标普通本地 CAN 总线是 0/1/2。

## 协议差异必须逐项迁移

| 契约 | SP098 本地旧库 | 锁定官方目标 | 设计要求 |
| --- | --- | --- | --- |
| `0xdd` 协商 | 3 字节，health/CAN/CAN-health=`16/4/5`，`10 04 05` | 8 字节 `<II>`，health hash/CAN hash | 严格区分长度和分支；不伪造版本匹配 |
| health | `<IIIIIIIIBBBBBHBBBHfBBHBHHB>`，58 字节 | `<IHHIIIIIHBBHBHBBHHHHB>`，48 字节 | 重新实现字段与语义，不能 memcpy 或补/删尾字节 |
| CAN health | `<BIBBBBBBBBIIIIIIIHHBBBIIII>`，64 字节，版本 5 | 相同字段布局/64 字节，由完整 health.h hash 保护 | 相同尺寸不代表协商或每个统计量实现相同 |
| CAN wire | 6 字节头、XOR；旧 packer 将 bit0 留零；默认分块超过256后换块 | 同为6字节头/XOR，byte0 bit0 表示 FD；可选 chunk，默认不分块 | F4 明确拒绝 FD/DLC>8，不能截断或降级；H7 保留 FD |
| USB framing | 所查旧 Python 没有每64字节计数头 | 官方明确没有每64字节计数头 | 按连续字节流和残包处理，USB packet 边界不是 CAN 帧边界 |
| 板型/MCU | DOS=6 在 F4 列表；F4 app 参考入口 `0x08004000` | 板目录及构建为 H7；app 参考入口 `0x08020000` | 恢复 F4 BSP 和 DOS 身份，不冒充 H7 |
| BYD safety 编号 | 32 | 35，registry 受 `ALLOW_DEBUG` 控制 | 不直接把 32 翻译成35宣称同等策略；宋 PLUS safety 尚需独立开发 |

目标版本算法为删除文件中的 CR，计算 SHA-256，取前4字节按小端整数解释。health.h 完整 SHA-256 为 `f512bcff6925e233a35c00e2d921fe01933fd77c0705c573a233b170d73ce20d`，版本 `0xffbc12f5`；opendbc CAN 头 SHA-256 为 `76f2ab751f89797c0a2254ab9a010a261678fe30f869d4e777cb7f2048802743`，版本 `0x75abf276`。目标 `0xdd` 字节必须是 `f5 12 bc ff 76 f2 ab 75`。32位协议 hash 只是 ABI 守卫，不能替代完整固件来源/签名/safety 版本证明。

新 health 将电压/电流由32位改为饱和16位，多个布尔值合并为 flags，interrupt load 从 float 改为 `u8/255`；新增 NMI/hardfault、声音幅度和温度，移除旧 fan stall 字段。温度字节解码为 `值−40°C`，基线没有“未知”哨兵：F4 若缺少已实现、校准的 provider，必须通过经审查的 capability/可用性语义标记，不能把0伪装为有效−40°C。SPI 两代字段名称/范围也不同，不能未经核查视为同一计数。新版和旧版完整 offset 清单保存在配套 JSON。

CAN wire 定义为 `byte0=DLC<<4 | local_bus<<1 | fd`，bytes1–4 小端是 `address<<3 | extended<<2 | returned<<1 | rejected`，byte5 为其余头和 payload 的 XOR。整个帧 XOR 为0；长度是 `6+DLC_TO_LEN[DLC]`，经典最大14、FD最大70字节。内存结构 `CANPacket_t` 声明 packed/aligned(4)，**预期 sizeof=72、data offset=6、checksum offset=5**；这是静态推导，尚未用两个 ARM 编译器目标验证。不能发送结构末尾 padding。

## 需要修改的模块

| ID | 模块/路径 | 具体改变 | 依赖或限制 |
| --- | --- | --- | --- |
| FW01 | application-only build graph：`panda/SConscript` | Separate app-only targets and per-MCU include/startup/linker config. Preserve explicit ALLOW_DEBUG provenance; do not reuse H7 flags on F4. Default graph currently also builds bootstub. | Complete pinned F4 BSP plus toolchain lock; signing compatibility remains unproven. |
| FW02 | F4 BSP and startup：`panda/board/config.h`, `panda/board/stm32f4/* (proposed restored sources)`, `panda/board/early_init.h` | Supply clock, interrupt, ADC, PWM, USB, timer, startup, vector relocation and linker implementation. Use explicit candidate profile; unknown clock/chip/pins remain null in deployable profile. | Full licensed historical F4 runtime is absent locally; select and pin complete source before implementing. |
| FW03 | DOS board adapter：`panda/board/boards/board_declarations.h`, `panda/board/boards/dos.h (proposed)`, `panda/board/stm32f4/board.h (proposed)` | Add DOS=6 without masquerading as H7. Map legacy initializer to new API (has_fan/LED PWM/bootkick/siren/thermal/harness), explicit feature capability. Treat DP pin table as reference only. Do not copy broad detection or infer 4 CAN controllers from 4 transceivers. | Reference-only pin profile and mock GPIO; hardware verification is later deployment condition, not design prerequisite. |
| FW04 | classic CAN backend：`panda/board/main.c`, `panda/board/drivers/bxcan.h (proposed)`, `panda/board/drivers/can_common.h` | Select backend by MCU; adapt bxCAN RX/TX/interrupt/errors to current CANPacket_t and current safety calls. Reject FD and DLC>8 before unsafe copy/queue. Local bus only 0..2. Preserve checksums, returned/rejected semantics and overflow counters. | F4 controller/filter/mailbox definitions from chosen BSP; never reuse old safety header. |
| FW05 | shared protocol and health：`panda/board/health.h`, `panda/board/main_comms.h`, `panda/board/can_comms.h`, `opendbc/opendbc/safety/can.h` | Keep canonical new headers shared; 0xdd returns exact current two hashes. Implement health providers and capability-aware host semantics. Transport reset clears only that board/session residue. Validate TX flags/range/checksum before any backend use. | Unsupported sound/temperature/FD stats need explicit absent semantics; zero is not proof of measurement. Do not change target hashes merely to accept old layouts. |
| FW06 | shared safety：`opendbc/opendbc/safety/safety.h`, `opendbc/opendbc/safety/declarations.h`, `panda/board/main.c`, `panda/board/drivers/*can.h` | Both MCUs use locked current RX/TX/FWD/tick and local bus IDs. Only MCU internal checked forwarding may bypass TX hook. Unknown mode and missed heartbeats return SILENT. Never expose skip_tx_hook to host. | Song PLUS-specific safety remains separate unimplemented work; BYD 32 to 35 is not a semantic conversion; debug BYD availability is not vehicle acceptance. |
| FW07 | resource profiles and linker：`panda/board/drivers/can_common.h`, `panda/board/stm32f4/*_flash.ld (proposed)`, `panda/board/stm32h7/stm32h7x5_flash.ld` | Keep canonical 64-byte payload and hash. Configure smaller F4 queues; validate Flash/RAM/stack/DMA/interrupt budgets per declared MCU. Preserve H7 memory layout and FDCAN behavior. | Physical MCU density and RAM not proven. Candidate queue count is tunable estimate, not validated capacity or throughput. |
| FW08 | timer and watchdog HAL：`panda/board/drivers/timers.h`, `panda/board/drivers/simple_watchdog.h`, `panda/board/main.c` | Implement calibrated 1us timer, wrap-safe elapsed, 8Hz loop and 1Hz safety tick. Preserve threshold boundary semantics and heartbeat fail-closed behavior. | Offline fake clock can prove logic, not oscillator accuracy/ISR jitter; software watchdog cannot independently detect all CPU stalls. |
| FW09 | USB identity and maintenance isolation：`panda/board/drivers/usb.h`, `panda/board/config.h`, `panda/python/__init__.py`, `panda/python/constants.py` | Separate per-MCU application metadata and USB capability catalog from reset/flash/recover. Shared protocol does not change each board role or global bus mapping. Firmware manifest binds MCU/profile/source/signature, not merely 32-bit wire hash. | Preserve installed bootstub; app USB VID may differ from existing bootstub. Host allowlist and maintenance workflow require explicit mode separation. |
| FW10 | offline tests and artifact manifest：`panda/tests/libpanda/*`, `opendbc/opendbc/safety/tests/*`, `downstream offline test/build manifest (proposed)` | Run pure ABI/codec/safety/HAL tests plus separate F4 and H7 app compile/link; never run discovery/constructor/reset/flash helpers. Record full hashes, compiler/link flags, maps, safety mode set and nondeployable status. | Implementation and all compile/test execution are future work. |

表中新增文件名是提议位置，不表示文件已存在。维护时以独立补丁序列保留官方基线；两块板的稳定身份、全局 bus offset 和角色在主机层处理，MCU safety 始终只看到本地 0/1/2。`skip_tx_hook` 只能由经过当前 forwarding 判定的 MCU 内部路径使用，不能开放成宿主“直通发送”选项。

## 内存、时钟和 bootstub 边界

当前 H7 队列为 RX4096、TX416×3。按预期72字节元素，仅元素占 **384,768字节（375.75 KiB）**，尚未含其他全局变量、栈、USB、安全状态。H7 把 RX 放 AXISRAM、两条 TX 放 ITCM，其余默认区域；`#else // kept for PC` 不是已经支持 F4。F4 必须重设队列和链接预算。

初始候选可以用 RX256、TX64×3，元素占32,256字节，但这仅是待评估的资源方案，**不是已确认本机容量或吞吐指标**。优先保留共同64字节 payload/头文件 hash，通过缩小队列节省内存；若改为紧凑队列，需另行设计与 canonical packet 的显式转换，不修改协议类型偷偷节省空间。Flash/RAM bank、DMA可访问性、启动复制表、向量、栈/中断嵌套、签名尾部都需独立 map 检查。H7 的地址/section/cookie 不可直接拷贝到 F4。

计时适配必须提供1μs时间基准、8Hz loop、1Hz safety tick，并保持 uint32 回绕计算。当前软件 watchdog 在 kick 时检测 `elapsed > 375000μs`；等于阈值不触发。心跳阈值为点火开5、关2，engaged mismatch 达3次退出 controls。`fault_occurred` 仅记录 fault/status，不会自行设置 SILENT 或 reset；SILENT 是另一个心跳过期路径的行为。模拟时钟可验逻辑，但不能证明真实晶振、IRQ 延迟，也不能把依赖 kick 的软件 watchdog 说成能独立检测所有 CPU 卡死。

当前 SConscript 的 `build_project` 同时生成 bootstub 和 app，必须为下游拆出明确 app-only 目标。旧 F4 库的 `0x08004000`、H7 的 `0x08020000` 只是参考入口；现有 bootstub 的实际内容、接受密钥/最低版本、向量和签名封装兼容性尚未证明。官方 bootstub.c 的长度/版本/RSA 检查只能用作接口模型。候选产物 manifest 必须 `deployable=false`，不含 bootstub 更新、provisioning/OTP 写入或绕过签名的方法。以后即使两个 MCU 编译通过，也不能据此批准刷入。

## 实施顺序与离线验收

下列全部为 **PLANNED / NOT RUN**；本轮完成的只有源码哈希、锚点范围、stdlib 布局尺寸和协议 hash 的静态核对。

| 阶段 | 交付物 | 依赖 | 对应验收 |
| --- | --- | --- | --- |
| F0 | Freeze ABI/source manifest and maintenance separation | 无 | FWA03, FWA16 |
| F1 | Select full traceable F4 source; write explicit candidate profile and fake HAL | F0 | FWA13 |
| F2 | Application-only F4/H7 targets and resource/linker profiles | F1 | FWA01, FWA02, FWA10 |
| F3 | Current wire and classic-CAN driver with per-MCU health providers | F2 | FWA04, FWA05, FWA06, FWA09 |
| F4 | Current safety, clocks and failure behavior integrated on both shims | F3 | FWA07, FWA08, FWA11, FWA12 |
| F5 | H7 regression plus reproducible candidate manifest, still nondeployable | F4 | FWA14, FWA15, FWA16 |

| 验收 ID | 输入与检查 | 通过条件 |
| --- | --- | --- |
| FWA01 F4 and H7 app compile/link | Build each declared MCU app independently with locked cached toolchain; compiler warnings fail; no downloads, USB access or bootstub/install target. Produce ELF/map/bin and manifest. | Both targets link under explicit profiles; incomplete F4 source means not run, never a stub pass. |
| FWA02 C/Python ABI | Compiler static_assert sizeof health=48, CAN health=64, CANPacket=72, offsetof(data)=6, checksum=5; all non-bitfield offsets match JSON. Validate bitfields by serialized byte vectors, not offsetof(bitfield). | Run for F4 ARM, H7 ARM and host shims; verify wire length is 6+DLC length, never sizeof(CANPacket_t). |
| FWA03 Version and source hash | Normalize CR exactly as baseline; assert health=0xffbc12f5, CAN=0x75abf276, reply f5 12 bc ff 76 f2 ab 75; mutate header/layout and verify mismatch. | Reject old 3-byte 10 04 05, unknown lengths and wrong hashes in target session; no fallback masquerade. Full source hashes tracked separately. |
| FWA04 Protocol boundary vectors | Classic 0..8 bytes, extended and standard limits, all DLC mappings for H7; fd bit, return/reject flags, XOR. Add invalid address, local bus 3..7 and simultaneous return+reject. | Known vectors exact; F4 rejects fd=1 and DLC>8, even short payload. Host-origin returned/rejected frames rejected. No truncation or FD demotion. |
| FWA05 USB stream assembly | Split every frame at each byte and across 63/64/65-byte USB boundaries; concatenate frames and inject corrupt checksum/incomplete tails. | No per-64-byte counter invented; parser never emits partial/bad frame; bounded buffers; reconnect/reset drops residue. Each board has isolated state. |
| FWA06 Health and capability semantics | Old58/new48/CANhealth64 independent fixtures; voltage/current saturation, flags, load rounding, temp offset, absent feature values. | Layout mismatch rejected; absence visible in capability-aware view; zero temperature byte must not be reported as a valid -40 C reading for absent sensor. Old fan stall not fabricated in new packet. |
| FWA07 Current safety parity | Run current safety suite with each backend shim and same current header; RX checksum/counter/lag/frequency, TX whitelist/relay, FWD block paths, nooutput/SILENT. | Identical safety decisions for F4/H7 classic vectors. All applicable suite cases pass; ALLOW_DEBUG build status recorded. BYD pass never labeled Song PLUS approval. |
| FWA08 No bypass path | Trace all host TX entry points and checked forwarding; test bad checksum, unknown mode, wrong local bus, relay malfunction, unsupported format. | No host-controlled skip_tx_hook, ALLOUTPUT or ELM327 workaround. Rejected count/flags/checksum consistent; invalid input never reaches hardware send stub. |
| FWA09 Queue safety and backpressure | Flood RX/TX, wrap producer/consumer indices, queue full/empty transitions, ISR/main interleaving model and canary memory. | No overwrite/use of uninitialized payload; overflow counters and backpressure observable. Test each F4/H7 queue profile, not only H7 large buffers. |
| FWA10 Link and memory bounds | Map assertions for startup/app/vector/stack/heap/static queues, data/bss/copy tables, bank visibility and flash reserved areas. | Nothing overlaps bootstub, provisioning, OTP or reserved cookies; all sections in declared MCU banks; F4 contains no assumed H7 bank addresses. Size growth budget recorded. |
| FWA11 Timers and safety elapsed | Fake clock advances through exact thresholds and uint32 wrap; verify 1us conversion, 8Hz tick, 1Hz safety_tick and timestamp arithmetic. | Threshold and wrap cases match current source; elapsed values cannot wrap into permissive safety decisions. Runtime clock accuracy remains hardware test. |
| FWA12 Watchdog/heartbeat failures | Inject tick delays at 374999,375000,375001 us; heartbeat loss on/off and engaged mismatch; IRQ starvation model. | Software watchdog records fault only for elapsed >375000 as current source; it does not itself set SILENT or reset. Separately verify heartbeat thresholds 5/2 enter SILENT and >=3 engaged mismatches clear controls. IRQ starvation model exposes inability to independently certify a halted CPU. |
| FWA13 Board GPIO contract using mocks | Run declared DOS reference mode/harness/power transitions against fake GPIO, include unsupported mode and feature-off cases. | Pin operations match declared reference and never silently map unknown profile to H7/DOS. Physical pin correctness remains unknown; no target execution. |
| FWA14 H7 regression | Unmodified H7 fixture corpus covers CANFD, BRS, error counters, queue bank layout and original SPI/USB protocol. | Shared changes keep H7 FD capability and expected source behavior; no F4 restriction leaks into H7. |
| FWA15 App/bootstub compatibility metadata | Inspect existing app file format and public bootstub contract without private key/provision reads; compare entry/length/version/minimum/signature public metadata. | Only compatible app format eligible for later bench planning; installed bootstub trust/MCU remains unproven => deployable=false. No signature bypass or bootstub replacement. |
| FWA16 Hermetic maintenance isolation | Audit build/test entry graph and mock imports for device APIs, subprocess flash/recovery, reset/GPIO/role writes, network downloads. | Offline CI cannot acquire device handles; firmware mismatch only quarantines. Build artifacts have no auto-install command. |

固件阶段属于总计划 P5，可在总计划 P1 后与主机 P2–P4 并行；这里 F0–F5 是固件内部依赖，不要求主机开发等待固件完成。

所有离线测试应分别报告 host shim、F4 ARM 和 H7 ARM 的范围；不能用宿主通过替代 MCU 链接，也不能用 H7 编译替代 F4。CI 使用 mock transport，禁止导入会自动打开/重置设备的入口，禁止下载、安装和 flash/recover 子进程。构建 manifest 记录完整源码/补丁/工具链哈希、MCU/profile、编译宏（特别 ALLOW_DEBUG）、app入口、ABI、内存预算、签名类型和尚未通过的实机条件。

本设计不完成车辆接线、宋 PLUS 专用 safety、驾驶员监控、目标系统启动或恢复验证。硬件信息缺失不会被填猜值，也不阻止上述离线阶段推进。

## 可复核源码锚点

所有 path 以工程根为基准。下表每条锚点精确引用配套 JSON 的 `sources[]` 文件 SHA-256、完整 commit 和行范围。旧 SP098 的 commit 为 null，明确保持为历史快照。

| Source ID | Commit / 来源 | 文件与 SHA-256 |
| --- | --- | --- |
| panda_build | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/SConscript](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/SConscript)<br>`b3544b66774e9a4456bbb17ff040e725460bb700a9ab2e4505e7dbafa81772f9` |
| health | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/health.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/health.h)<br>`f512bcff6925e233a35c00e2d921fe01933fd77c0705c573a233b170d73ce20d` |
| main_comms | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/main_comms.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/main_comms.h)<br>`57c717ac0434b16e6026e46d006d72a89be8661d9e51c3e1a99719b524c0a203` |
| python | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/python/__init__.py](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/python/__init__.py)<br>`20815dd095ba76bdb9d095e33ddbba62a7ccabf7bd4e87bdc61dee147e9fb04a` |
| can_comms | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/can_comms.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/can_comms.h)<br>`5806c9bda986d0044b6cb83ed0b79ae95b715b75f0f6fb33cbfc6e2e660841b3` |
| can_count | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/can.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/can.h)<br>`ee30d6a723476e5bfaf5493249af715c0f783b95fe4cdc19cb868da09b2334e8` |
| can_common | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/drivers/can_common.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/can_common.h)<br>`1509297d8ce4f8a527c3bf941dae3f82a26db82a4c3397fd324bd32b81ec9ec1` |
| fdcan | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/drivers/fdcan.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/fdcan.h)<br>`c2dc76a42e039414b1f8b384cec8e3eae47f8f4eeea68d92150b843598f6f150` |
| main | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/main.c](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/main.c)<br>`c72ebafaf2891ccc7ed447db6a1839f85c1d2c4d77f92f01a77ff2db882360bc` |
| timers | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/drivers/timers.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/timers.h)<br>`5328d20470d4fc43b327ccf0be5bd41e91ccdf4d2d97230cbfa345870ae58fa4` |
| watchdog | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/drivers/simple_watchdog.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/simple_watchdog.h)<br>`38cd9992ac95ec35dd22a24a2896ec453407d33b98ddb9ff06b5b1a1a85d20d3` |
| boards | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/boards/board_declarations.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/boards/board_declarations.h)<br>`536032eee2fac73314af7dfb105df81fca33bf05f87094f11ee30af8de778241` |
| config | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/config.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/config.h)<br>`a9814cb0ef7939d65507809995d704c55f9e4dfb2411934f44fa8e829d2d48e2` |
| h7_linker | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/stm32h7/stm32h7x5_flash.ld](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/stm32h7/stm32h7x5_flash.ld)<br>`2da174976f3e3abf0542e78549a4a12a20df39023a56b1c5a23ae41958e95894` |
| bootstub | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/bootstub.c](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/bootstub.c)<br>`c363f70286b1591805de82f830c7f3b0a6b9cd9ba53eb0be4bf023535f0582ef` |
| usb | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/drivers/usb.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/usb.h)<br>`dd520b2567206dce35ab24bb27fbc432ee5be6111142943bac088dc5a802dd92` |
| can_packet | `b3bd79a659dae961af169a3baac723961f720133` | [upstream-openpilot/opendbc_repo/opendbc/safety/can.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/opendbc_repo/opendbc/safety/can.h)<br>`76f2ab751f89797c0a2254ab9a010a261678fe30f869d4e777cb7f2048802743` |
| safety_defs | `b3bd79a659dae961af169a3baac723961f720133` | [upstream-openpilot/opendbc_repo/opendbc/safety/declarations.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/opendbc_repo/opendbc/safety/declarations.h)<br>`f6a6d2466c1131b734b763a9d65cdbd51bcf66f876e14bd662be5f8c55b0b284` |
| safety | `b3bd79a659dae961af169a3baac723961f720133` | [upstream-openpilot/opendbc_repo/opendbc/safety/safety.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/opendbc_repo/opendbc/safety/safety.h)<br>`48523444b74f68181a8ef16afc74e790862591632d49d760c01271292c952c6e` |
| dp_build | `94ad85cfda64773a960d048dc11cac02d4aa86a5` | [research/evidence/public-source/dragonpilot-94ad85cf/panda_tici/board/SConscript](/Users/shuang/Code/openpilot/songplus-port-lab/research/evidence/public-source/dragonpilot-94ad85cf/panda_tici/board/SConscript)<br>`5f09764e1a72c17f9dc7f95f0a5f17a4faca601cacd886f3fc4c3fdfc10111d3` |
| dp_dos | `94ad85cfda64773a960d048dc11cac02d4aa86a5` | [research/evidence/public-source/dragonpilot-94ad85cf/panda_tici/board/boards/dos.h](/Users/shuang/Code/openpilot/songplus-port-lab/research/evidence/public-source/dragonpilot-94ad85cf/panda_tici/board/boards/dos.h)<br>`ec15964f5d3cb106a0fb5932fb34d71e1d2bd5d01780c979eaeec471318a42ed` |
| dp_f4 | `94ad85cfda64773a960d048dc11cac02d4aa86a5` | [research/evidence/public-source/dragonpilot-94ad85cf/panda_tici/board/stm32f4/board.h](/Users/shuang/Code/openpilot/songplus-port-lab/research/evidence/public-source/dragonpilot-94ad85cf/panda_tici/board/stm32f4/board.h)<br>`f65f4c1d45a1a56a8b0c70f166424778d13a68708981d48b0954f1e85c899c5b` |
| sp098_python | `unknown: local SP098 snapshot` | [../op_rootdir/data/openpilot/panda/python/__init__.py](/Users/shuang/Code/openpilot/op_rootdir/data/openpilot/panda/python/__init__.py)<br>`c2df6b142adb4ae1a4163128760a8e03a4659eae349b3f7b062231cec7a762c7` |
| sp098_mcu | `unknown: local SP098 snapshot` | [../op_rootdir/data/openpilot/panda/python/constants.py](/Users/shuang/Code/openpilot/op_rootdir/data/openpilot/panda/python/constants.py)<br>`a2ed3128ea0b2e83613638e00c8da7b169a27575b7380bd4efe85bca26a23761` |
| faults | `a59195163c32a3a375ea6c86715dc08fd0984c1b` | [upstream-openpilot/panda/board/sys/faults.h](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/sys/faults.h)<br>`c4df5dbedf40c7f30231b99a095dd4950152aafaa8dfcc7c056598f57916bb3b` |

| Anchor ID | Source 与行 | 证明内容 |
| --- | --- | --- |
| build_h7 | [panda_build:67–141](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/SConscript:67) | Only H7 project definition; include path, startup, app entry and bootstub/main targets are coupled. |
| version_hash | [panda_build:159–174](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/SConscript:159) | Header SHA-256 first four bytes little endian after removing CR; H7 targets only. |
| health_layout | [health:3–64](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/health.h:3) | Packed health and CAN health fields and health flags. |
| health_encode | [main_comms:9–54](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/main_comms.h:9) | Saturation, flags, interrupt-load and temperature scaling. |
| version_reply | [main_comms:222–231](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/main_comms.h:222) | Safety control and two-u32 0xdd response. |
| new_python_codec | [python:34–118](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/python/__init__.py:34) | Six-byte CAN header, XOR checksum, fd bit, optional chunking, strict versions. |
| new_python_catalog | [python:125–155](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/python/__init__.py:125) | VID pair, H7-only catalog and header-hash definitions. |
| new_python_health | [python:518–595](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/python/__init__.py:518) | health decoding; CAN health uses shared health hash. |
| new_python_versions | [python:617–621](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/python/__init__.py:617) | 0xdd reads exactly eight bytes, unpacked as two little-endian u32. |
| usb_stream | [can_comms:8–146](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/can_comms.h:8) | Wire framing has no per-64-byte sequence counter; stream assembly and reset. |
| can_abi | [can_packet:3–20](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/opendbc_repo/opendbc/safety/can.h:3) | Canonical CANPacket_t has 64-byte data, packed and aligned(4). |
| can_bus_count | [can_count:1–5](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/can.h:1) | Target firmware has three ordinary CAN buses. |
| queues | [can_common:16–39](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/can_common.h:16) | 4096 RX and three 416 TX queues; H7 bank section placements. |
| tx_checksum_safety | [can_common:144–176](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/can_common.h:144) | XOR and TX safety/rejection path; internal skip hook flag exists. |
| h7_driver | [fdcan:88–218](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/fdcan.h:88) | H7 FDCAN TX/RX, checksums and forwarding. |
| main_driver_safety | [main:13–79](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/main.c:13) | FDCAN include and unknown-mode SILENT fallback. |
| heartbeat | [main:101–103](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/main.c:101) | Ignition-on/off heartbeat thresholds are 5 and 2. |
| heartbeat_failures | [main:187–248](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/main.c:187) | Heartbeat mismatch, expiry and 1Hz safety tick behavior. |
| main_time_init | [main:293–311](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/main.c:293) | 1-us timer initialization; 8-Hz watchdog loop threshold is 375000 us. |
| timer_hal | [timers:1–33](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/timers.h:1) | Timers depend on APB timer frequency constants. |
| software_watchdog | [watchdog:5–23](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/simple_watchdog.h:5) | Kick detects elapsed time strictly greater than threshold; not independent hardware watchdog. |
| board_interface | [boards:13–60](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/boards/board_declarations.h:13) | New board function/feature layout and hardware-type catalog. |
| mcu_config | [config:1–26](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/config.h:1) | H7 config include and registered USB VID. |
| h7_memory | [h7_linker:56–80](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/stm32h7/stm32h7x5_flash.ld:56) | H7 startup cookies, app start, stack and memory banks; not F4 geometry. |
| app_signing_contract | [bootstub:30–80](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/bootstub.c:30) | Current reference checks app length/version/signature before jump; not proof of installed old bootstub. |
| usb_identity | [usb:460–468](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/drivers/usb.h:460) | USB bcdDevice high byte is software hardware type. |
| new_byd_id | [safety_defs:30–40](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/opendbc_repo/opendbc/safety/declarations.h:30) | Current SAFETY_BYD is 35. |
| safety_rx | [safety:199–227](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/opendbc_repo/opendbc/safety/safety.h:199) | Current RX checks and common safety processing. |
| safety_tx_fwd | [safety:241–287](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/opendbc_repo/opendbc/safety/safety.h:241) | Current TX whitelist/relay protection and local-bus forwarding contract. |
| safety_tick | [safety:320–340](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/opendbc_repo/opendbc/safety/safety.h:320) | Microsecond timestamps and lag/frequency enforcement. |
| safety_registry | [safety:391–423](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/opendbc_repo/opendbc/safety/safety.h:391) | BYD entry gated by ALLOW_DEBUG; no proof of Song PLUS readiness. |
| dp_dual_mcu | [dp_build:1–18](/Users/shuang/Code/openpilot/songplus-port-lab/research/evidence/public-source/dragonpilot-94ad85cf/panda_tici/board/SConscript:1) | F4 and H7 referenced but base_project_f4 implementation absent in this snippet. |
| dp_dos_pins | [dp_dos:9–128](/Users/shuang/Code/openpilot/songplus-port-lab/research/evidence/public-source/dragonpilot-94ad85cf/panda_tici/board/boards/dos.h:9) | Reference transceiver mux, fan/IR/bootkick/harness pins; not measured device facts. |
| dp_dos_interface | [dp_dos:130–158](/Users/shuang/Code/openpilot/songplus-port-lab/research/evidence/public-source/dragonpilot-94ad85cf/panda_tici/board/boards/dos.h:130) | Legacy DOS interface differs from current board struct. |
| dp_lite_detect | [dp_f4:8–42](/Users/shuang/Code/openpilot/songplus-port-lab/research/evidence/public-source/dragonpilot-94ad85cf/panda_tici/board/stm32f4/board.h:8) | Reference F4 HAL and Lite-as-DOS detection comment; not independent manufacturer confirmation. |
| old_can_codec | [sp098_python:26–104](/Users/shuang/Code/openpilot/op_rootdir/data/openpilot/panda/python/__init__.py:26) | Old CAN-v4 Python uses six-byte header, XOR and byte0 bit0 cleared. |
| old_byd_id | [sp098_python:140–148](/Users/shuang/Code/openpilot/op_rootdir/data/openpilot/panda/python/__init__.py:140) | Historical SAFETY_BYD is 32. |
| old_catalog_versions | [sp098_python:160–187](/Users/shuang/Code/openpilot/op_rootdir/data/openpilot/panda/python/__init__.py:160) | DOS type 6, F4 catalog and versions 16/4/5 with exact struct formats. |
| old_health_decode | [sp098_python:621–695](/Users/shuang/Code/openpilot/op_rootdir/data/openpilot/panda/python/__init__.py:621) | Historical health fields including float interrupt load and fan stall count. |
| old_version_reply | [sp098_python:722–729](/Users/shuang/Code/openpilot/op_rootdir/data/openpilot/panda/python/__init__.py:722) | Historical 0xdd expects three bytes: health/CAN/CAN-health versions. |
| old_mcu_profile | [sp098_mcu:27–54](/Users/shuang/Code/openpilot/op_rootdir/data/openpilot/panda/python/constants.py:27) | Historical F4 app 0x08004000/H7 0x08020000 library profiles; no physical chip proof. |
| watchdog_fault_record | [faults:8–19](/Users/shuang/Code/openpilot/songplus-port-lab/upstream-openpilot/panda/board/sys/faults.h:8) | fault_occurred records status/bits; does not itself set SILENT or reset MCU. |
