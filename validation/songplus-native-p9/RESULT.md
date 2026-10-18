# P9 原生发现、USB/SPI provider 与绑定校验

结论：**本阶段离线实现与验收完成，真实 profile 仍为 UNBOUND，禁止部署。**
没有连接 C3XL、枚举真实 Panda 或打开真实 spidev，没有修改系统、切槽、重置、刷写或发送真实 CAN。
使用已有本地 Lima VM 和缓存工具，没有下载依赖、推送代码或触发远程 CI。

## 交付内容

1. 原生 USB 完整库存发现：限定正常固件候选，精确读取 UID、板型、ABI、完整应用签名，
   仅已知 F4 签名允许能力扩展。额外、重复、不可访问、短回复、超预算均拒绝。
2. USB 保留同一句柄；激活要求当前 lease 的接口证据并重新探测身份。发现不 claim/写控制；
   绑定桥始终只读，禁止所有 OUT 和有副作用的 vendor IN，不因单板拒绝而绕板发送。
3. Linux SPI provider：精确节点/字符设备号/现有配置验证，完整事务持有 flock，单次全双工 ioctl；
   不设置配置或 GPIO。异常、取消、时钟回退、短传输及解锁失败关闭会话。
4. SPI 同 fd 身份探测和只读 lease；保留实际 selector、初始请求 ID 及探测时间，防止 handoff
   更换节点、冒用请求 ID 或给旧探测重新标时。
5. profile 校验与现有 runtime owner 接口：固定两槽位、精确身份、能力/角色及外部证据校验。
   缺证据保持 UNBOUND；通过的合成测试为独立 `NATIVE_READ_ONLY_BOUND` 候选，不能获得控制权限。
6. 独立 P9 patch、维护锁、源代码重放、离线构建入口、CI 及证据发布门槛；原有 SPI 路径保留。

原生代码实际编译/链接，所有执行的 USB/SPI 调用均由测试注入。Linux 额外链接实际 `main.cc`、
原 Panda/SPI 和全部新 provider，仅运行原有 offline 入口及活动设备参数拒绝测试。
这不是 AGNOS/目标板运行验证，也不是一次完整生产 SCons 构建。

## 本轮正式验收

有效批次：`results/p9-native-full-003`，UTC 2026-10-18 06:26:39 至 06:33:14。
维护源码提交：`4b2f7c78644a7622ef3a70bd6dfbededfe573ae3`。
下游 host patch 提交：`06fd6e68223c6ad6caabc7f64d2f032445a42095`，20 个变更文件。

| 测试 | macOS | Linux |
|---|---:|---:|
| USB 发现 / provider | 30 组 / 1454 断言 | 30 组 / 1454 断言 |
| SPI 原生 I/O | 25 组 / 805 断言 | 25 组 / 805 断言 |
| SPI 同 fd 身份探测 | 15 组 / 1915 断言 | 15 组 / 1915 断言 |
| profile / lease / runtime 只读交接 | 32 组 / 334 断言 | 32 组 / 334 断言 |
| **P9 新增合计** | **102 组 / 4508 断言** | **102 组 / 4508 断言** |
| P8 runtime 回归 | 28 组 / 1903 断言 | 30 组 / 1937 断言 |
| P0–P6 native、daemon、双板故障回归 | 222 组 + 5 个 SPI golden | 222 组 + 5 个 SPI golden |

以上正式测试零失败、零跳过，使用 ASan/UBSan。Linux 实际 pandad 新链接成功，offline owner
返回未绑定/不可用，`--compat-native`、`--compat-runtime`、`--compat-device` 均拒绝。
每个平台的 222 组包括 native 168、runtime 22、loop 10、复合故障 22；SPI golden 独立计数。

两次源码重放均为 306 文件，归档哈希一致。Mac 本轮源码 CI、独立 Linux 源码 CI 各通过
7 条命令 / 56 项维护测试。Linux CI 从测试提交的 348 文件归档运行，不含官方检出、
local、results 或缓存依赖目录；没有自动安装或联网。远程托管 CI 未执行。

编译器、链接库及额外头文件记录了本轮前后哈希（Mac 853、Linux 327 项），Linux 包含
spidev 头文件；P4 缓存依赖继续按既有锁验证。额外系统头记录是本次构建来源与稳定性证据，
不能称为跨机器固定 SDK 或两个平台二进制逐字节可复现的证明。

P8 `full-002` 的 ARM 固件、资源与 98 段车辆回放仅作为复核通过的**历史前置证据**；
本轮新 ARM 构建 0、新车辆回放 0。新原生测试与 P8/P0–P6 host 回归均重新执行于 P9 源码。

独立 release-check 重新核对提交、锁、补丁、逐项日志、二进制、时间归属和测试范围后，返回：
`BLOCKED_FOR_DEPLOYMENT`，预期退出码 **3**，`offline_native_complete=true`。

## 基线与证据

四个官方检出 HEAD 和 tracked 文件均保持不变：

- openpilot：`35e78a9d9518855ec0dacec919e719624a28131f`
- Panda：`a59195163c32a3a375ea6c86715dc08fd0984c1b`
- opendbc：`b3bd79a659dae961af169a3baac723961f720133`
- msgq：`0e266c1dbcf7328beee3e57b4a8688555387c877`

P7 锁：`3ee3704aeaad414e0024f7e4c246b58cea84c54a970ddba2ee3c4a346b6c5806`。
P8 锁：`1ee1afdd5a7931128f1f3aaaca1facdef4570858ea1dcb1815556f4c024d2838`。
P9 锁：`d8b3bb3c42a394f178f3ed8325a2276d38f28ff15596459e8fe8806eeb98e7eb`。

[receipt.json](receipt.json) 和 [evidence-index.json](evidence-index.json) 是版本化索引。
实际验收必须保留 `results/p9-native-full-003`、`results/p9-source-linux-003` 和锁定的
P8/P7 历史结果树；单独复制 receipt 或验收 JSON 不足以通过发布门槛。

前期 `full-001` 在构建前因旧 P8 门槛函数参数不匹配失败，已修复并加入真实签名回归。
`full-002` 在 Mac 回归途中主动停止，用于补严源码 CI / 重放的新执行时间及子测试范围检查。
两者保留记录，不计入通过结果；最终 `full-003` 从最新提交完整重跑。

## 保持未绑定的条件

当前没有真实证据校验器、生产启动入口或持续发现调度。只读桥拒绝 runtime 初始化控制写，
因此通过本阶段测试也不能获得健康/安全屏障通过、接合或车辆发送权限。

[实机核实条件清单](../../native_maintenance/HARDWARE_GATES.md) 给出逐项验收标准，主要顺序是：

1. 恢复链、两块板的精确 MCU / PCB / UID / 固件与 ABI、完整供电和拓扑。
2. 当前 USB alt0/claim 条件，SPI 节点、设备号、mode/bits/speed、独占及跨进程请求 ID 新鲜性。
3. 目标 Linux 内核 I/O 最坏耗时、取消、拔板/断电/重连、双板故障与外设/总线路由。
4. 后续运行许可、驾驶员监控与告警、资源最坏上界、宋 PLUS 控制器和专用 safety 的独立验证。

描述符存在 alt0 不证明当前处于 alt0；UID/签名回复不证明物理型号或启动信任；
O_NONBLOCK 和离线超时测试不证明同步 ioctl 可被硬截止时间抢占。

## 维护入口

```sh
upstream-openpilot/.venv/bin/python -B tools/songplus_native.py verify-lock
upstream-openpilot/.venv/bin/python -B tools/check_songplus_native_ci.py --mode full \
  --linux-instance byd-recovery-311 --linux-root /home/shuang.guest/songplus-p2-build \
  --output results/p9-native-next
upstream-openpilot/.venv/bin/python -B tools/songplus_native.py release-check \
  --report results/p9-native-full-003/validation/result.json
```

输出目录必须不存在；命令不连接 C3XL，不提供刷写、系统修改或生产启动操作。
