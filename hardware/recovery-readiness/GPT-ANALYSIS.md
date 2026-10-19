# sde GPT 的本地证据分析

**结论：已精确重建采样时的主备 GPT 小表及头部，并把备用表校验异常定位到头部保存的 entries CRC。只在内存中验证了两个 CRC 字段的最小修改范围。恢复链尚未验收，没有生成可刷写 payload，也没有授予修复、切槽或启动权限。**

分析使用 2026-10-17 留存的六个小型 GPT 文件、10 月 19 日 `recovery` / `recovery-detail` 采样，以及主线程单独执行的只读头部采样。最终采样时间是 **2026-10-20 00:52:46 +08:00**（UTC `2026-10-19T16:52:46.705706+00:00`）。本文“当前”仅指该采样窗口，不代表此后的设备状态。各次采样不是原子磁盘快照。

本地分析没有连接设备，没有读取分区大镜像或私有配置。只读采样入口与其 receipt 单独留证；入口只读固定 sde 元数据，未安装目标文件或执行目标服务控制。JSON 数值、范围和逐源 SHA 见 [gpt-analysis.json](gpt-analysis.json)。

## 1. 已验证的条目变化

GPT 表为 **64 项 × 128 字节 = 8192 字节**，其中 48 项占用、16 项未用。

| 内容 | 2026-10-17 小表 | 10/19、10/20 采样对应内容 |
|---|---|---|
| entries SHA-256 | `4f43d094ebcbcebdced44488a8b96070727f3c77e4ebf1c8ff434714b55d173b` | `799842357d5111d451fe234f30dbbf4636bf6fb6b8dc263aa6a499adef20fcc5` |
| entries CRC32 | `0x6b2cbd8e` | `0x6f40dffc` |
| 主备 entries | 字节完全相同 | 完整 SHA 相同，并精确匹配内存重建结果 |

重建仅做了两类变化：

1. 按 10/19 逐项记录更新分区 1–34 的 **34 个 attributes 字段**。
2. 按同名 `_a` / `_b` 配对交换 **17 对 type GUID 字节**，即 `(1,18)` 到 `(17,34)`。每一项都先核对目标 type GUID 的哈希等于历史对侧字段哈希；双方必须互换，不能从任意同类型分区猜取。

两类变化共涉及 **574 个实际不同的字节**。unique GUID、起止 LBA、名字原字节、扩展字节、全部未用条目保持不变。脚本逐项核对所有分段哈希、占用索引、未用项的“索引＋条目”哈希，最后要求整个 8192 字节表 SHA 严格匹配采样。任一不符都会终止分析，不接受“看起来相似”。

这证明采样记录与该字节变换一致；**不能证明是谁、何时、为何更改了 A/B 元数据，也不把历史 B 槽状态恢复为目标。** 本轮没有运行 `abctl` 或切槽。

## 2. 新采样补齐了头部证据

10/19 两份证据只保存 entries 指纹和 CRC 有效性，没有保存完整 header SHA。仅凭它们无法做当前头部的精确修改演算。最终 [gpt-headers-bounded.json](evidence/20261020/gpt-headers-bounded.json) 记录标准数值字段、完整 4096 字节 sector SHA、disk GUID / 保留 / 扩展 / 填充区哈希，以及两种 CRC 的保存值和计算值，不输出 GUID 或原始 payload。

00:49 的首次采样使用的 collector 只检查表读取范围位于整盘内，没有在读取前把范围限制到已批准的 GPT 元数据区。审查后保留了 [首次证据](evidence/20261020/gpt-headers.json) 和 [原 collector 快照](evidence/20261020/gpt-header-collector-original.txt)，并修正为：打开磁盘前要求固定的 4096 字节扇区 / 4 GiB 几何；读取任何表之前严格核对主备位置、互指 LBA、可用范围、header CRC / 长度 / revision、64 × 128 表形状及固定表范围。任一不符即拒绝读取表。

首次记录中的实际表地址是主 `2`、备 `1048571`，均位于批准的元数据区，没有指向分区内容。修正后于 00:52 重新只读采样，**主备所有 sde 字段与首次记录一致**。最终分析使用修正后的样本、receipt 和 collector SHA；首次采样仅作为带有已知范围校验缺口的审计历史保留，不能代替修正后的验收。

分析以历史原始 header 为内存基底，只替换新采样明确观察到的数值字段；所有其余区域的哈希必须一致，最后整个 sector SHA 必须一致。**主、备两个头部均达到完整 SHA 精确匹配。** 这一步使用了完整哈希约束，没有猜造未知 header 字节。当前原始 sector 没有作为二进制文件导出或存入工程。

| 检查 | 主头部 | 备用头部 |
|---|---|---|
| 头部所在 LBA | 1 | 1048575 |
| alternate LBA | 1048575 | 1 |
| entries LBA | 2 | 1048571 |
| header SHA-256 | `77327e5fe6c84a455166e8bacab871c23abf6f724a39a73fc151ff26bde1b7c3` | `e79c29003568e334c5b9fe9dea03e76c26e3b00b84cc4d5bbd433e620b2306d6` |
| 保存 / 计算 header CRC32 | `0x4d2e7765` / 相同 | `0x22419cc1` / 相同 |
| 保存 entries CRC32 | `0x6f40dffc` | **`0xbfd6628d`** |
| 计算 entries CRC32 | `0x6f40dffc` | **`0x6f40dffc`** |
| entries CRC 校验 | 通过 | 不通过 |

逐表读取前后的 header 字节相同；这只证明各自读取窗口内 header 稳定，不能把跨主备、跨日期的读取说成原子快照，也不能证明 entries 读取窗口之外没有发生变化。

还验证了以下结构条件：

- 磁盘范围 `4294967296` 字节，逻辑扇区 `4096` 字节；主备所在位置和 alternate LBA 对应。
- 两个 header 的 revision、长度、disk GUID 哈希、可用范围与条目形状对应；保留字段为零。
- 可用 LBA 范围为 `6–1048570`，两套 GPT 元数据位于可用范围之外，且未越过磁盘边界或相互覆盖。
- 48 项分区均在可用范围内、无范围重叠、unique GUID 非零且不重复；全部分区范围和 attributes 与 10/19 主备记录一致。

这些是 GPT 元数据一致性检查，不验证任何分区内的文件系统、镜像、签名、启动链或设备寿命。

## 3. 仅在内存验证的 CRC 字段差异

在上述完整 SHA 均匹配之后，分析才进行当前备用头部的 CRC 演算：保留其他所有字节，把 entries CRC 调整为该表的计算值，再重新计算 header CRC。结果如下：

| 字段 | 采样时值 | 内存演算值 |
|---|---|---|
| entries CRC32 | `0xbfd6628d` | `0x6f40dffc` |
| header CRC32 | `0x22419cc1` | `0x9d8cc933` |
| 完整备用 header SHA-256 | `e79c29003568e334c5b9fe9dea03e76c26e3b00b84cc4d5bbd433e620b2306d6` | `ee5855e6557424f2f557f86858adb45b3ee3180dd986398a5c1143337e17d1a1` |

实际不同的字节只有 **header 内偏移 `16–19` 与 `88–91`，共 8 字节**，分别属于 header CRC 和 entries CRC 两个字段。内存结果两项 CRC 均正确，其他字节完全保留。没有改变 entries、A/B attributes、分区类型、GUID、LBA 或填充区。

这里的“最小”限定为让现有表内容与标准 CRC 字段一致所需的两个字段范围；它不是经过设备恢复演练的修复方案。没有保存这 4096 字节演算结果、没有生成写入程序或写盘命令。报告中的派生 SHA 仅是可复核的数学结果，`deployable`、`recovery_ready`、写入授权均为 `false`。

10/17 历史备用头部也单独完成相同的内存检查，派生 SHA 是 `5ebde9ee87394dd477ffbba7165b99f89e7116644d39be6eac50c6c597fcc32c`。**历史表和头部已不同于本次采样，历史候选不能直接套用。** 原归档中的历史候选文件没有被读取、导入工程或用于当前演算。

## 4. 验收及可重复运行

在本机执行了 **34 个纯离线测试，34 通过、0 失败、0 跳过**。测试使用合成数据和注入 I/O，覆盖合法解析、短读、异常尺寸/边界、CRC 不符、整表 SHA 不符、分段字段变化、未用项变化、非互换 type GUID、重复索引、分区越界/重叠、重复 unique GUID、当前头部数值重建、未观察字节变化、读取窗口变化、仅两 CRC 字段差异、输出不含二进制值，以及模块导入不执行 I/O。新增范围验收覆盖主备固定位置、28 个恶意字段子例、指向分区内部的有效 CRC header 在任何表读取前被拒绝、错误几何在打开磁盘前被拒绝，以及 mock collector 只读取六个预期的 header / table 范围。主线程另外执行的入口和 supervisor 测试不混入本节的 34 项计数。

以下命令都只操作本地证据；分析命令只向 stdout 输出脱敏 JSON，不提供二进制输出选项：

```sh
python3 hardware/recovery-readiness/test_gpt_offline_analysis.py
python3 hardware/recovery-readiness/gpt_offline_analysis.py \
  --header-evidence hardware/recovery-readiness/evidence/20261020/gpt-headers-bounded.json \
  --header-receipt hardware/recovery-readiness/evidence/20261020/gpt-headers-bounded-receipt.json
```

需要保留原归档中的六个小文件，位置固定为工程旁的 `migration-audit/recovery-backup-20261017/gpt`。分析不扫描该目录其他文件，更不读取分区大镜像。输入必须是有界、非符号链接的普通文件；archive SHA、旧证据 receipt / collector SHA、新证据 receipt / collector SHA 都必须匹配。

关键采样文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| 10/19 `recovery.json` | `c4f492750310bf2ef8aa81769dea9ad243f6dd696befe68ebda957055fc343eb` |
| 10/19 `recovery-detail.json` | `d095816e305e4c901ebd8a4a6a996a88d9445ad98268251cbcf82e4cb74876ea` |
| 10/20 `gpt-headers-bounded.json` | `39a519b532591ae3eb498fe3e76cb7034f97b834a469a532d48dde1ddbcd6b65` |
| 10/20 `gpt-headers-bounded-receipt.json` | `eaa8c205dbe3cac26ef6e3fc63378683e3687992fcc9bee0be197b2dfd45285d` |
| `gpt_header_collector.py` | `65e38d73872a507a37c1240548a40efbbfbf18811fe524b837d8987c35af95a0` |

完整来源清单还包括三个历史 header、三个历史 entries、两个旧 receipt、相关 collector、本地分析/测试源码、首次头部采样及原 collector 快照，均列在 JSON 的 `sources` 中。没有用旧测试结果冒充本轮验收。

## 5. 仍未解决及后续前置条件

本轮可以证明采样时 GPT 字节与哈希相符，并解释备用 entries CRC 为什么不通过。仍不能证明备用头部为何保存了另一 CRC、相关 A/B 变化由谁触发、系统是否还能更新这些元数据，或 CRC 修正后是否可正常启动。

任何未来涉及设备变更的方案，都需要另行形成可审查方案及明确授权；至少应满足：

1. 重新读取并绑定操作时刻的完整主备头部、entries、几何信息和槽位状态；任一哈希或状态改变就停止沿用本次演算。当前数据只是样本，不能作为未来写入前检查的替代。
2. 在工程外保留并核验恢复所需原始备份、可信镜像、签名/设备适配关系；本轮的哈希报告不是恢复介质。必须覆盖恢复入口、主机工具和失败后的回退条件，不能仅凭 GPT 两项 CRC 通过就认定恢复链完成。
3. 对持续运行的软件可能修改 A/B 元数据的风险形成有证据的处置方案。此次授权没有启停服务、重启或切槽，尚未验证稳定维护窗口。
4. 完成独立的恢复链审查与隔离台架准入；具体未满足项以 [RECOVERY-PLAN.md](RECOVERY-PLAN.md) 和恢复门槛文件为准。本分析不自动解锁原生设备探测、运行控制、固件刷新或车辆测试。

下一步仍应推进恢复材料与隔离台架条件的准备，而不是自动修复 sde。
