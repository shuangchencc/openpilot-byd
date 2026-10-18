# P5 离线验收：DOS/F4 应用 BSP 与候选固件

状态：**PASS_OFFLINE_CANDIDATE**。P5 的 A50–A55 已完成；这不是实物、部署或车辆验收。
完整机器结果见 [result.json](result.json)，冻结设计逐项证据与限制见 [acceptance.json](acceptance.json)。
本轮未连接 C3XL/Panda，未复位、切槽、刷固件或修改车机系统。

## 基线与交付

| 项目 | 固定版本 |
|---|---|
| openpilot | `35e78a9d9518855ec0dacec919e719624a28131f` |
| Panda | `a59195163c32a3a375ea6c86715dc08fd0984c1b` |
| opendbc / safety | `b3bd79a659dae961af169a3baac723961f720133` |
| 现有 P0–P4 host 补丁头 | `4d374354317a65c0b23895533f23b403cc226518`，本轮未修改 |
| P5 Panda 补丁 | `aa02e42356f12b5ed4f0dbf553ffc2e8a7211b65` |
| 完整官方 F4 历史源 | `3dc21386239e3073a623156b75901aa302340d6c` |
| ARM 编译器 | `comma-deps-gcc-arm-none-eabi 13.2.1.post103`，实际 GCC13.2.1 |

补丁在 [patches/panda/p5](../../patches/panda/p5/README.md)，校验入口为
[tools/validate_panda_firmware.py](../../tools/validate_panda_firmware.py)。官方仓库、gitlink、
旧阶段补丁及报告保持不变。正式运行从锁定 Git 源重新建立干净目录并应用补丁，不使用工作树的未提交内容。
完整工具链文件、协议/源文件与 P4 manifest 都校验哈希；运行后再次校验，macOS Seatbelt 禁止网络、
设备节点和 IOKit 打开。此前公开源码下载与正式离线验收分开。

已落地 startup/中断向量、96 MHz 参考时钟、GPIO、ADC/温度校准、USB OTG-FS、fan/tach、
DOS harness/供电参考引脚、bxCAN、当前 health/CAN 协议与当前 safety 集成。102个外部IRQ槽包括95个实现和7个保留槽。
F4新linker独立编写并使用MIT许可；未复制受限的历史Atollic linker。完整来源清单和各源文件许可证保留在补丁内。

F4拒绝FD/DLC>8，保留72字节官方CANPacket；没有复制H7的大队列。未知strap清空板型和指针，
在执行板级输出和LED初始化之前拒绝未知板。ADC采样有界，采样或工厂校准失败时整包health不可用。
电流、声音幅度、SPI错误计数由独立0xd5扩展声明不可用；纯离线解码显示为`None`。
IR不配置，未加入虚假的SPI、音频或电流传感器。

## ARM 编译与资源

四个真实 Cortex-M4/M7 应用都完成 `-Wall -Wextra -Werror` 编译、链接、ELF/map/bin输出、
独立栈使用量/调用图检查及公开开发签名格式校验。没有构建bootstub。

| 候选 | 原始应用 B | 带版本与签名 B | RAM已分配 B | 栈预留 B | 最大单函数帧 B |
|---|---:|---:|---:|---:|---:|
| F4 debug | 70,160 | 70,296 | 86,840 | 8,192 | 288 |
| F4 非debug | 68,096 | 68,232 | 85,960 | 8,192 | 224 |
| H7 debug | 79,176 | 79,312 | 456,545（多bank合计） | 1,024 | 288 |
| H7 非debug | 77,144 | 77,280 | 455,665（多bank合计） | 1,024 | 224 |

F4采用尚未实物确认的 **STM32F413xG / DOS公开参考配置**：Flash保守上限1MiB，应用从`0x08004000`开始，
保留16KiB前区和136B版本/签名预算；只分配低128KiB SRAM，其余192KiB不使用。
F4 debug静态数据为78,640B，静态上限122,872B；另有8KiB栈与8B cookies。
CAN队列元素总计32,256B（RX256、TX64×3；环形队列可用槽255/63）。H7保持原队列384,768B及bank布局。

验收用真实linker验证：恰好达到RAM/Flash边界可以链接；分别超出4B，region overflow和显式栈/签名边界断言均拒绝。
F4启动向量、copy/zero区、堆栈和两个cookie不重叠；F4 USB使用CPU FIFO，没有SPI DMA。
H7保留byte DMA，SPI缓冲位于SRAM12，不能错误要求其按word对齐。

栈检查包含单函数帧与已知直接调用链，但仍有间接/未解析调用，**未证明最坏中断嵌套栈深度**。
H7原布局还存在`enter_bootloader_mode=0x38001ffc`位于`mic_tx_buf[0x38001770,0x38002770)`中的事实；
本轮未改变该布局，报告明确披露重叠，未验证其运行影响。F4无该对象重叠。

## 实际执行结果

| 验收 | 本轮结果 |
|---|---|
| MCU ABI | F4/H7两个真实ARM目标；另6个旧ABI负编译均按预期拒绝 |
| 当前协议/health/USB分片/队列 | F4 15组、2497断言；H7公共协议15组、2495断言 |
| 实际F4 bxCAN + LL寄存器模型 | 24组、745断言 |
| 实际H7 FDCAN模型 | 14组、29,074断言，包括FD、BRS、全部DLC、错误、队列和FWD |
| 实际F4 GPIO/ADC/温度/fan/定时器 | 17组、90断言；另6项编译/链接边界检查 |
| 实际候选C heartbeat/watchdog oracle | F4/H7各13组、343断言 |
| 缺失能力解码 | 12项，实际提取固件0xd5字节，canonical health布局校验 |
| 当前opendbc safety | 重新编译debug/release hooks，49项通过 |
| P0–P4宿主Python回归 | 90项通过 |
| 宿主C++ codec/独立跨语言oracle | 13组、47,031断言，C++/Python/ARM线格式一致 |
| 宿主传输及原SPI对照 | 59组，其中SPI16组；5个原实现黄金向量一致 |

所执行测试零失败、零错误、零跳过。4个官方BYD不适用方法及1个空方法在机器报告中列为未选，
未计入通过；49项仅为defaults/framework/BYD及显式政策回归，**不是所有车型全量safety套件**。
BYD35仅为官方Atto3 debug参考，非debug构建拒绝该debug-only模式；未把旧32/33翻译成新safety配置。

HAL/register mocks不会访问硬件；H7测试副本仅替换两处地址宽度和一处FIFO ACK副作用，生产FDCAN源保持原样。
heartbeat/watchdog oracle执行候选实际C，但HAL与safety-hook调用为明确stub；真实safety行为由独立套件验证。
超时软件watchdog仅记录fault，不能独立处理已停止的CPU；进入SILENT由单独heartbeat逻辑处理。
宿主SPI回归不等于MCU SPI电气验证。未重新声称P4的Linux daemon构建为本轮结果。

## 候选与后续边界

两个构建policy都用仓库公开development key验证文件格式。`release`只表示未定义ALLOW_DEBUG，
**不是发布签名，也不说明已安装bootstub信任此签名**。所有产物`deployable=false`，没有安装包或刷写命令。

还缺实物MCU/引脚/振荡器/USB/电气与目标运行时确认、bootstub信任/恢复链、最坏栈与水位验证。
0xd5能力扩展的离线解码已完成，但P4真实运行路径尚未绑定；不能用P4现有power-owner读取逻辑把缺失电流解释为有效0mA。
实物profile仍未绑定。无驾驶员相机、完整音频告警、宋PLUS safety/车辆适配仍须后续解决；P6/P7未整体完成。
