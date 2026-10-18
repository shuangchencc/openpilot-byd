# P7 离线维护与验收结果

P7 已完成维护封装、干净补丁重放、可复现候选构建、统一验收入口、CI 契约和发布门槛。P0–P7 的 **41/41 项冻结离线条件通过，0 失败、0 跳过**。这仅表示所声明的离线兼容范围完成；不表示 C3XL、Panda 实物、恢复或宋 PLUS 车辆可部署。

- 本轮：`p7-76aa82bd7265438da9e386106fe689d6`；开始 `2026-10-18T04:38:47.399464+00:00`，完成 `2026-10-18T04:45:41.569132+00:00`。
- 维护代码：`568a6317da3d7720cb632748ee852691c2583062`；锁：`3ee3704aeaad414e0024f7e4c246b58cea84c54a970ddba2ee3c4a346b6c5806`。
- 官方 openpilot `35e78a9d9518855ec0dacec919e719624a28131f`；Panda `a59195163c32a3a375ea6c86715dc08fd0984c1b`；opendbc `b3bd79a659dae961af169a3baac723961f720133`；msgq `0e266c1dbcf7328beee3e57b4a8688555387c877`。
- 主机补丁仍为 P6 `d4d610ecb08ab27200e48c07757b147861f710a5`，Panda 补丁仍为 P5 `aa02e42356f12b5ed4f0dbf553ffc2e8a7211b65`。P7 是外层维护工程提交，不修改这些源码或官方 gitlink。

## 新增交付

统一命令为 `tools/songplus_compat.py` 的 `verify-lock`、`replay`、`validate`、`release-check` 和 `inspect-runtime`，用法见 [维护说明](../../maintenance/README.md)。下游锁覆盖 76 个维护/补丁/依赖定义文件；完整检查核对 824 个源码输入、311 个缓存依赖文件。

从干净目录重放六个 host 补丁和一个 Panda 补丁。host 的全部声明编译输入为 286 个文件，完整 Panda 232 个，未修改的 opendbc 454 个。两次源码内容包 SHA256 相同。源码包把模式统一为 0644，不是完整系统安装包。

运行时包装只检查明确的合成身份和本地候选文件。未知板型、协议、签名、缺失产物和哈希漂移均被拒绝；没有下载、自动编译、启动、安装、DFU、USB角色切换、重置或刷写分支。直接操作 spies 与审计 hooks 的正向控制证明拒绝测试确实覆盖这些调用。

## 本轮实际执行

| 范围 | 执行结果 |
| --- | --- |
| P7 维护逻辑 | 128 项通过，0 失败/跳过 |
| CI 契约检查 | Mac与Linux均128项维护+21项CI通过，0失败/跳过；共享完整入口通过 |
| P5 ARM | F4/H7 × debug/release，四个候选从新目录编译；资源与签名检查通过 |
| P5 协议/BSP | ARM ABI 2、预期失败编译 6、协议 30 组、bxCAN 24、FDCAN 14、BSP 23、能力 12、liveness 26 |
| P5 host/safety | Python 90；codec 13组/47,031断言；transport 59组+SPI5；精选官方 safety49 |
| P6 Mac/Linux | 每平台 native222组（168+runtime22+loop10+复合故障22），0失败/跳过 |
| P6 safety | 每平台64组/5,086断言、30项经典帧一致性比较；四个F4/H7 debug/release策略 |
| P6 Python/消息 | Python94；codec13组/47,031断言；每平台11条实际cereal消息/59项跨语言检查 |
| P6 Linux | 真实 main.cc/pandad/Panda/SPI 编译链接，仅显式 offline 入口；8项参数负测正确退出2 |
| 第二组 ARM | 在不同 Panda、opendbc 和输出目录再次编译四个候选，并逐个重新审计资源 |

本次不重复第0步整套感知/模型编译，不构建AGNOS系统镜像；相关历史报告保留，P7聚焦兼容层已声明的构建输入。上述统计单位不同且部分重叠，不能相加宣称独立测试总数。预期失败的 ABI/BSP 编译和参数拒绝是通过的负测，不是跳过或未解释的失败。P5/P6 报告来自本轮新执行，旧报告只用于开发期格式核对，不计入本轮通过数。

## 可复现产物

| 配置 | 原始bin字节 | 签名bin字节 | 两轮bin及signed | ELF字节相同 |
| --- | ---: | ---: | --- | --- |
| f4-debug | 70160 | 70296 | 相同 | true |
| f4-release | 68096 | 68232 | 相同 | true |
| h7-debug | 79176 | 79312 | 相同 | true |
| h7-release | 77144 | 77280 | 相同 | true |

实际完成八次 ARM 应用编译。两轮的原始与签名镜像逐字节相同，版本、源身份、协议和 MCU 元数据一致；逐项 SHA256 见 [复现证据](evidence/repro.json)，12个保留ELF/bin/signed文件见 [产物清单](evidence/artifacts.json)。带绝对路径的 map、依赖或日志文件不要求相同，也没有跨工具链或整个目录字节一致的结论。

所有配置仍使用官方公开开发签名密钥，`release` 仅指编译时关闭 debug safety 的策略，不是可安装的受信发布。没有构建或替换 bootstub，没有开启实物权限。

## CI 与发布门槛

[CI配置](../../.github/workflows/panda-compat-offline.yml) 将源码契约和预置环境完整验收分开；本地实际运行共享完整入口，见 [CI报告](ci-full.json)。另在本地Linux真实user/net/mount沙箱执行只含锁定源码的CI契约；9条命令通过，128项维护测试和21项CI测试均通过，源码包不含官方checkout、依赖缓存或.git，见 [Linux源码CI报告](ci-linux-source.json) 和 [证据哈希](ci-linux-source-evidence.json)。GitHub工作流未推送、未远端触发，仓库变量、runner和保护environment需要在将来单独配置，不能视为已经生效的云端保护。

正常 `release-check` 重新核验本轮报告/证据/日志的哈希、时间区间、case集合、两个MCU的构建记录和实际产物。当前返回 **退出码3**：离线完整、部署被阻止。证据缺失或漂移返回2；不把任意非零退出当作成功。`offline_compatibility_complete=true` 与 `deployment_ready=false` 并存。

八类阻塞仍保留：实物身份和接线、C3XL目标运行、已安装bootstub/签名/恢复链、缺失驾驶员相机所需的监控方案、音频告警、宋PLUS控制器与专用safety/实车语义、运行资源风险，以及候选信任链。资源风险包括最坏嵌套栈未知和继承的H7 cookie与麦克风缓冲区重叠，静态预算通过不能消除这些问题。

本轮未连接 C3XL，未进行设备枚举、实际 CAN TX、系统或槽位修改、Panda重置/刷写、硬件MCU执行或车辆测试。后续实物/恢复/目标机/车辆验收必须另立证据，不能从41项离线通过推导。

## 复核与证据位置

完整本机运行：`results/panda-ci-p7-20261018-002/validation/result.json`，亦由 `results/panda-compat-p7/latest.json` 指向。完整入口命令和Linux缓存位置见维护说明。本目录保存原始聚合报告、证据JSON和顶层日志；大二进制与各编译工作目录留在忽略的results/，因此本目录不是可发布包，直接对它做release-check会因缺少产物而拒绝。

[聚合报告](result.json)、[逐项验收](acceptance.json) 与原有P5/P6报告并存；冻结设计文件中的NOT_RUN、旧阶段报告中的remaining字段保留历史含义。新的运行不复用旧latest.json计数，不覆盖旧失败记录。首次P7全流程在所有编译/回归通过后，由于旧A23全suite映射的空selector被新汇总器漏计，正确以失败退出；修正为读取两平台实际执行group，新增正/负回归后，重新从完整入口运行本报告批次。[首次失败记录](prior-failed-run.json)保留，不计作本次通过。
