# 第 1 步补充：固定挂载、槽位标记和恢复材料

本报告结合本地静态审查与主任务在 2026-10-18 01:41（Asia/Shanghai）取得的补充只读采集。本报告分工自身未连接设备，也未运行 `abctl`、`bootctl`、设备库、恢复程序或挂载命令。旧报告保留不变；新结论单独记录在这里。

用户明确说明 C3XL 去除了驾驶员相机和电话卡功能；此项标为**用户陈述**。USB 中出现 modem 接口不能据此推导仍具备可用电话卡/4G 功能。

## 已有挂载现象可以解释，尚不能据后缀判错

保存的 `/etc/fstab` 明确包含：

```text
/dev/disk/by-label/dsp /dsp auto ro
/dev/disk/by-partlabel/modem_a /firmware auto ro
```

**补充实机观察已证实**：当前 fstab 的大小和 SHA-256 与历史文件相同；`/dev/disk/by-label/dsp` 指向 `/dev/sde9`，与 dsp_a 相同；modem_a 指向 `/dev/sde4`。这与当前 `/dsp`、`/firmware` 的挂载设备一致，根系统仍为 system_b。因此可以确认：`/firmware` 按配置固定选择 modem_a，`/dsp` 按 label 选择 dsp_a。不同后缀有配置解释，不能单独作为损坏或需要切槽、修 GPT 的证据；这也不证明官方 19.9 能运行。

归档中的 `ab_mount.sh` 另有 `getslotsuffix`、向第三参数追加后缀、等待符号链接后执行 mount 的逻辑。这只证明存在支持 A/B 的挂载助手；本次有限启动脚本检索未证实 `/dsp`、`/firmware` 由它负责。没有运行该脚本。

## 不调用 abctl，也能有限解释槽位标记

未找到保存的 `abctl` C++ 源码，但可以对保存的 `libabctl.so.0.0.0` 进行静态反汇编。其 SHA-256 为 `6516e1da564d22729ad3c4fad05edfaa9d810308d76cfe4f51b9eabf43e18843`，大小 60,160 字节。

具名函数与字面量共同证明它读取 `/dev/sde` 的 boot_a/boot_b GPT entries：

| 字段 | 精确静态依据 | 仅对该版本的解码 |
|---|---|---|
| active | getActiveStatus 的 `0x2980` 指令提取 bit 50 | `(attributes >> 50) & 1` |
| success | getSuccessStatus 的 `0x2fd0` 指令提取 bit 54 | `(attributes >> 54) & 1` |
| unbootable | setUnbootable 的 `0x3088` 置 bit 55；SetBootSuccess 的 `0x28b4` 清 bit 55 | `(attributes >> 55) & 1` |

本轮实机 `libabctl` 和 `abctl` 的大小、SHA-256 已与本地归档逐一匹配，因此下列有限解码绑定到了实际安装的库。其他版本不匹配时应只保留 `attributes_hex`。未知位不命名为 priority/retry，也不推导完整 bootloader 选择算法。当前启动后缀可直接从 `/proc/cmdline` 过滤提取。没有证据需要盲读 `misc+2048` 或套用另一种 Android 元数据布局。

本轮读到的属性与 10/17 保存值一致，可按已匹配库解释为：

| 分区 | 保存的属性 | active | success | unbootable |
|---|---|---:|---:|---:|
| boot_a | `0x0071000000000000` | 0 | 1 | 0 |
| boot_b | `0x0077000000000000` | 1 | 1 | 0 |

**success=1 只是保存的标记，不能代替本次启动或恢复演练。**

## 本地实际有哪些恢复材料

- 13 份历史块镜像、软件/配置备份与 Panda application/bootstub 文件仍按原报告保存。完整性历史验证与实际恢复能力仍须区分。
- Android platform-tools 的 zip、fastboot、adb 和来源记录已存在；没有证明 C3XL 可通过这些程序恢复。
- SP2027 应用工厂压缩包仍存在，大小 252,010,377 字节；历史验证 SHA-256 为 `71b22356c7444d077d7011c489c08d2fa7faf6d51da06495fe936e9a7ca62fff`，本次只检查存在和大小。它不是已验证的完整 C3XL 系统/启动链/loader 恢复包，也不是当前 SP098 的等同镜像。
- 在 JSON 列明的解包目录中按 qdl、edl、prog_firehose、loader ELF/MBN、rawprogram XML 等文件名检索，未找到候选。该否定结论只针对指定文件名和目录，未逐一打开全部归档，也不能排除不同名称的材料。openpilot 的 Firehose 上传 UI 与 Qualcomm 恢复 loader 无关。
- 官方 Panda README、recover.py 和内部 Panda 重刷脚本确实存在，但会构建、复位、刷写，后者还写 GPIO。它们不是只读板型检查程序；本次未执行，不能视为已验证适用于现有板卡的恢复流程。

厂家当前 [恢复页面 post=40](https://mr-one.cn/?post=40) 和 [post=14](https://mr-one.cn/?post=14) 都提供同一个 [Bilibili 视频入口](https://www.bilibili.com/video/BV1521sYmEeJ/)。已提取到视频入口，视频内容访问失败，**没有观看或核实其中步骤**；取得的 HTML 未发现 zip/elf/mbn 等下载链接。旧 post=24 返回 404。不能说厂家“没有恢复内容”，也不能把已找到的视频当成完成了底层 EDL、loader 或本机适配验证。

用户所述无驾驶员相机/电话卡功能，也得到 [当前产品系列页面](https://oneclone.net/product/c3x-lite/) 的无 DM/4G 描述支持；该页宣传 8 GB 与本机内核约 4 GB 观测存在差异，不能凭系列页确定本机硬件修订。

完整厂家恢复步骤、板型匹配 loader、实际数据口/供电方式、恢复模式主机可达性与完整恢复演练仍未闭环。

## 13 份块备份具体覆盖什么

这里依据历史采集元数据与 GPT 分区映射说明覆盖范围；本轮没有读取大块设备或重算大镜像哈希。上次步骤已证明当前 GPT entries 与保存的历史 entries 相同。

| 原始块备份 | 具体覆盖 | 范围限制 |
|---|---|---|
| system_a、system_b（2 份） | 两个完整命名系统分区，每个 12,437,225,472 字节 | 不是整个 sda，也不代表两个槽都已启动验证。 |
| sdb、sdc（2 份） | 各完整 LUN；分别含 xbl_a/xbl_config_a 与 xbl_b/xbl_config_b | 保存的是采集时启动链原始字节，恢复传输和可启动性尚未演练。 |
| sdd（1 份） | 完整 LUN，含其 GPT 与 cdt 等现有分区 | 未因此证明底层恢复流程或板型 loader 可用。 |
| sde（1 份） | 完整 4 GiB LUN，含 boot_a/b、abl_a/b、aop_a/b、devcfg_a/b，以及 modem、dsp 等该盘全部分区 | 同时保留了采集时 sde 备份 GPT header 的 entry CRC 异常，不能自动当成应原样写回的恢复配方。 |
| sdf（1 份） | 完整 LUN，覆盖其现有分区 | 内容保留在私有原归档；本次不读取或输出其中配置。 |
| persist、misc、keystore、frp、ssd、systemrw（6 份） | 六个单独共享分区的原始镜像 | 不是全机原子快照。 |

**没有完整 userdata（/data）和 cache 原始镜像，也没有整个 sda 原始镜像。** 软件/配置归档只覆盖选定内容，录像、日志和部分临时数据不在其中，不能代替完整、原子的 /data 备份。GPT 另保存小型原始快照。

**Panda 不在这些主机 UFS 镜像内。** 保存的 5 个 Panda 文件是 application/bootstub/version 软件产物；不是实际 MCU flash/bootstub/OTP/provisioning 的完整读取，也没有证明 DFU 物理连接或 MCU 恢复成功。因此“主机启动链已有备份”和“所有部件都能恢复”必须分开判断。

## 本轮已补齐的严格只读证据

1. fstab 相关挂载行和整文件哈希；ab_mount.sh 哈希及相关逻辑片段。
2. dsp label 与 dsp_a/b、modem_a/b 符号链接指向；仅输出有关挂载和当前 root/slot 参数。
3. 复用 O_RDONLY 有界 GPT 读取，增加 A/B 分区 64 位 attributes；不输出 GUID 或完整原始分区。
4. abctl 与 libabctl 的文件大小和 SHA-256；只读文件，不执行、不加载。

上述四项已由主任务补充采集，报告哈希与 receipt 一致，无需为这些问题再次访问设备。没有读取 misc、私有配置、IMEI 或 provisioning。

另有运行状态观察：USB role 从前次的 none 变为 ufp/device/sink，但 UDC 仍为 `not attached`，gadget 的 UDC 绑定仍为空。这不能证明某一物理口已对应恢复接口，也不能证明 EDL/Fastboot 可达。只读采集未调用设备控制；SSH 与既有服务可能产生正常日志。后续恢复测试仍是另一项会改变运行状态的工作。

## 来源

来源路径相对原始工作区；仅记录出处与小文件哈希，不复制私有归档。

| ID | 路径 | SHA-256 |
|---|---|---|
| `archived_fstab` | `op_rootdir/etc/fstab` | `f27372e58c79b74cbfe19e2d82c46b3e453cc959fe48debbb784665ec1917605` |
| `archived_ab_mount` | `op_rootdir/etc/initscripts/ab_mount.sh` | `f67c4bdb6e07c5ff8b0b12501a2f2d7c778852f9af84fc2688f3ecb91bdf697e` |
| `archived_abctl` | `op_rootdir/usr/sbin/abctl` | `499b2ce8b0de9b1bef13a58a6c467ac5a8700b5502efa6b31619906eacc0de53` |
| `archived_libabctl` | `op_rootdir/usr/lib/aarch64-linux-gnu/libabctl.so.0.0.0` | `6516e1da564d22729ad3c4fad05edfaa9d810308d76cfe4f51b9eabf43e18843` |
| `archived_sde_entries` | `migration-audit/recovery-backup-20261017/gpt/sde-primary_entries.bin` | `4f43d094ebcbcebdced44488a8b96070727f3c77e4ebf1c8ff434714b55d173b` |
| `platform_tools_provenance` | `migration-audit/recovery-tools/provenance.json` | `10042be93210877728a75fa89e2497054aeb66989d6e01fb39dfc826e57a0328` |
| `factory_verification` | `migration-audit/sp2027-review-20261017/factory-download-verification.json` | `459b0254c0038c3237b828fbf10d4a64638045a2f57013e2b994bd49cf83b96d` |
| `factory_summary` | `migration-audit/sp2027-review-20261017/factory-evidence/factory-inspection-summary.json` | `3674d5f77589f63bc20c4de3d3a9a0bf14706e6d923919f2a48fcc08c63ee58e` |
| `old_recovery_matrix` | `migration-audit/recovery-backup-20261017/recovery-decision-matrix.json` | `d4692388918dbf99ad24d2c8006cb99043b3625e40589a31522727e86b56f03a` |
| `official_panda_recovery_docs` | `songplus-port-lab/upstream-openpilot/panda/board/README.md` | `6e0d92688fa903d0cf2168f789905b5cf2ecdf5a49a4455c8fb2ae9584c91381` |
| `official_panda_recover` | `songplus-port-lab/upstream-openpilot/panda/board/recover.py` | `b174da677200dfde0ffbf09dd24935a529b38ed200d0cab006c8b3e542563f58` |
| `official_internal_panda_reflash` | `songplus-port-lab/upstream-openpilot/panda/scripts/reflash_internal_panda.py` | `bd470445b09808a034bfbed645dc22ef9ffebeb6a091ad3dab6581253a2b9528` |
| `official_agnos_updater` | `songplus-port-lab/upstream-openpilot/openpilot/common/hardware/comma/agnos.py` | `00f984081409417824f68c22b5d33d7239a8b123fb04a23e6dcd0a9553f866fc` |
| `original_live_system` | `songplus-port-lab/hardware/step1/evidence/live-system.json` | `2c81b0ff07859a545d587bed8dde970e64a23af6082dbebf12f5514e9e709fc1` |
| `original_live_recovery` | `songplus-port-lab/hardware/step1/evidence/live-recovery.json` | `04f47cff0f89158de9ec725c7ebc1cbab53bd32c7922cc22ff3ed6b9057b721e` |

补充证据（相对工程目录）：

| ID | 路径 | SHA-256 |
|---|---|---|
| `followup_live_evidence` | `hardware/step1/followup/live-evidence.json` | `22da230dbb412a866f05bfe0583daad6703bd19b12f9873791c97e0e3f5a6611` |
| `followup_live_receipt` | `hardware/step1/followup/live-receipt.json` | `4ca23b8488983df60c668ee034f1a186bb1b4a8e356d71fcc6e3052a76c0f2a3` |
| `followup_vendor_sources` | `hardware/step1/followup/vendor-sources.json` | `dc36a3874af7bc0463e223f95015f80084a4dbb935ac57750700ccb5c17cc359` |
| `backup_preparation` | `migration-audit/recovery-backup-20261017/preparation-result.json` | `4e2fafb4bf633acaf0d697656d6a49d010fafadadc0cc9480cf733895be1d593` |
| `prior_recovery_prerequisites` | `songplus-port-lab/hardware/step1/recovery-prerequisites.json` | `9832bea37fc397442424d9c616cd2b39dccacc5f134eba2b064d7fac4f2e6bcb` |
