# P8 固件资源整改：离线验收

状态：**PASS_OFFLINE_CANDIDATE**，实机资源发布门槛仍为 **BLOCKED**。
当前 Panda 补丁提交为 `c2aa3df08fe4864c9e4ecb4b4bbd150117ce2397`，接在
P5 `aa02e42356f12b5ed4f0dbf553ffc2e8a7211b65` 之后。官方基线、P5 历史补丁和
manifest 未修改。机器摘要见 [resources-result.json](resources-result.json)，
追加补丁与复验入口见 [P8 说明](../../patches/panda/p8/README.md)。

## 已修复的确定性缺陷

H7 原 boot cookie `0x38001ffc` 位于麦克风 TX 缓冲内；原 linker 的绝对符号没有
占用保留区。新应用 linker 保持两个 cookie ABI 地址，以独立 4B NOLOAD 区隔离。
SRAM4 低区放音频/麦克风 RX（8096B），高区放两个 TX（6096B）；所有缓冲仍在
SRAM4，维度、DMA 宽度、双缓冲半区对齐及处理函数保持不变。

H7 现有 1024B 栈改成位于 DTCM 顶端的真实保留区，512B heap 与静态数据不能越入；
F4 8192B 栈补充首尾断言。应用 linker 均保留 136B 版本/签名尾部，并验证 startup
copy/zero 范围。新增的是明确选择的 H7 **application-only** linker，原共享 bootstub
linker 和 generic SCons 未改，也不属于本轮验收入口。

## 本次实际执行

从锁定 Git 基线分别重放 P5+P8 到两个独立源目录；每个目录都新编译 F4/H7 ×
debug/release，完成 **8 次真实 ARM 编译和 8 次资源审计**。四配置的 raw、signed、
ELF 共 **12 对产物逐字节一致**。map、日志和调用图中的路径不声称相同。

| 候选 | raw B | signed B | RAM 已分配 B | 栈保留 B | 最大单帧 B | 已知直接链 B |
|---|---:|---:|---:|---:|---:|---:|
| F4 debug | 70160 | 70296 | 86840 | 8192 | 288 | 460 |
| F4 release | 68096 | 68232 | 85960 | 8192 | 224 | 396 |
| H7 debug | 79184 | 79320 | 456553 | 1024 | 288 | 468 |
| H7 release | 77152 | 77288 | 455673 | 1024 | 224 | 468 |

H7 RAM 为多 bank 合计，不代表一块连续可用内存；以上容量仍是参考候选预算，不是
对用户实物 MCU 的鉴定。所有签名使用公开开发密钥，包括 release 编译策略。

新增 **23 项资源检查**通过：12 项真实 linker 精确边界/溢出检查，8 项单帧、无界帧、
直接链溢出和递归证据拒绝，2 项缺失运行时证明的明确拒绝，1 项原 H7 对象/cookie
重叠注入拒绝。后两类拒绝成功表示门槛有效，不表示运行时安全验证通过。

同一最终批次还执行了：F4/H7 协议 30 组（4992 断言）、2 个 ARM ABI 和 6 个旧 ABI
负编译、bxCAN 24 组、FDCAN 14 组、BSP 23 组、能力 12 项、精选 safety 49 项，
以及两目标 heartbeat/watchdog 26 组。零失败、零错误、零跳过。49 项 safety 是
defaults/framework/BYD 和显式政策回归，不是全部车型测试，更不是宋 PLUS 专用 safety。

全部 compiler/test worker 在禁止网络、设备节点和 IOKit 的 macOS Seatbelt 下运行。
锁定输入、两份源树与 opendbc 原始文件在运行前后复核；上游 libsafety 写在临时源树
旁的两个 `tmp*.os` 编译对象单独记录，不把生成物当成源文件修改放行。

## 保留门槛与过程记录

已知直接链只是部分栈需求。间接调用、汇编帧、异常/FPU 上下文、IRQ 优先级与嵌套，
以及目标实测栈水位仍未证明。报告逐项保留未解析调用点；H7 的 `TIM1_UP_IRQHandler`
是启动汇编的弱别名，没有 GCC 帧，明确列入未解析入口，不能按 0B 填充。
`--require-runtime-proof` 返回 3 / `BLOCKED_RUNTIME_PROOF`，无放行开关。

最终结果来自 `results/songplus-integration-p8-resources/formal-4/result.json`。
此前 formal-1 因 runner 解析 venv 符号链接而丢失 cffi 失败；改为保留 venv 路径后
formal-2 全部通过。新增完整 IRQ 入口核查后，formal-3 暴露上述汇编弱别名，改为
明确记录未知后重新完整执行 formal-4。各次结果及失败日志保留，未把旧通过数充作新结果。

本轮没有访问 C3XL/Panda，没有 MCU 执行、CAN 发送、reset、刷写、系统修改或 bootstub
构建。物理身份/容量、安装信任与恢复链、真实 DMA/音频及完整最坏栈证明仍需后续门槛。
