# 第 1 步：恢复条件核对（历史证据与本轮对照）

现有备份材料可定位，**尚不能认定已经具备可靠、无损的恢复路径**。本文件先审查 2026-10-17 保存的本地证据并复核小文件，再对照主任务于 2026-10-18 01:28（Asia/Shanghai）取得的本轮只读采集。此报告分工自身没有连接设备、执行旧采集/恢复脚本、读取密钥或解包完整配置，也没有重新哈希大型块镜像。历史状态与本轮结果在下文分开标记。

## 已证实的范围

| 项目 | 历史证据及本次本机复核 | 不能据此推出 |
|---|---|---|
| 系统与关键分区备份 | 10/17 20:06 完成 13 份镜像；原始总量 30,975,074,304 字节，压缩总量 3,163,905,414 字节。采集时远端读取流与本地解压 SHA-256 相符，之后压缩包哈希验证通过。本次仅确认 13 份文件仍存在且大小相符。 | 本次没有重新证明大型文件内容完整，也没有证明可写回、可启动。 |
| 软件与配置 | 软件归档 7,503 项；首次归档有 params 在采集中变化的警告。随后单独核验最新配置 182 项，成员哈希匹配、采集中未观察到变化，但不是原子快照。 | 不是整个 userdata 原始镜像；录像、日志和部分临时内容未覆盖。 |
| A/B | 历史运行 B 槽 AGNOS 10.1，A 槽显示 12.4；A 槽 6 个启动链镜像在指定长度上符合 cp099 清单，system_a 前 5 GiB 不符。 | A 槽没有启动成功实测，不能当成已验证回退系统；A/B 不隔离共享数据或 MCU 固件。 |
| GPT | 本次重新验证保存的 6 个 LUN 主/备 GPT 小文件：所有 header CRC 有效；仅 sde 两个备份入口的 entry CRC 不符，主/备 entries 内容相同。这与原始分析一致。 | 不是本次实机 GPT 状态；离线修复候选从未应用，不能自动写入。 |
| Panda 文件 | F4/H7 application、F4/H7 bootstub 和 version 共 5 个小文件本次 SHA-256/大小均符合历史 manifest；文件标签为 DEV-e26a80db-DEBUG。 | 这是保存的软件文件，非 live MCU dump；不能证明实际 bootstub、OTP/provisioning 或 DFU/恢复连接。 |
| USB 恢复线索 | 历史 Linux 具有 a600000.dwc3 UDC 与 gadget 配置，采集时未绑定；物理接口映射未证实。 | 不能证明某一外部接口可用于 EDL、fastboot 或正确 loader。 |

较早 `rollback/audit-result.json` 中“尚未备份原始系统镜像”已被随后完成的备份取代。其“恢复未验证”的结论仍成立。A 槽旧清单校验是前 5 GiB，完整备份是 12,437,225,472 字节，不能直接比较两者哈希判断后续改动。

旧 AGNOS 下载地址仅有历史 HTTP 可访问记录。后续审查的 SP2027 应用工厂包声明 0.10.1 / AGNOS 12.4 / Panda DEV-b0cd5291-RELEASE；这与旧 SP098 备份不同，不等于已取得原系统精确工厂恢复镜像、设备适配 loader 或完成恢复。

## 本轮只读对照（2026-10-18 01:28）

本轮证据为 `evidence/live-system.json` 和 `evidence/live-recovery.json`，由主任务只读采集。本报告引用其哈希，未再次连接设备。

- **系统与槽位**：当前 AGNOS 10.1、Linux 4.9.103+，`slot_suffix=_b`、`root=/dev/sda7`，根文件系统以只读方式挂载。A 槽 12.4 仍只是历史结论，本轮未读取 A 槽系统内容或尝试启动。
- **GPT**：6 盘、12 个主/备 header 的 CRC 全部通过。唯一 entry CRC 异常仍是 sde 备份 header；主、备 entries 都是 `4f43d094ebcbcebdced44488a8b96070727f3c77e4ebf1c8ff434714b55d173b`。6 盘主/备 entries 各自 SHA-256 均与 10/17 保存的对应数组相同，其他盘全部正常。本轮未修复。
- **保存文件对应关系**：当前 5 个 Panda application/bootstub/version 文件、`build.json` 和 `camerad` 共 7 个选定文件均匹配历史哈希。它们是磁盘文件，不能证明实际 MCU 内部内容、整个软件树未变，或相机能够正常出图。
- **启动固件配套**：当前根系统为 B，但 `/dsp` 挂载 `sde9`（dsp_a），`/firmware` 挂载 `sde4`（modem_a）。这说明不能仅根据 root 的 B 后缀推断所有固件都来自 B；需要核实该板的启动和挂载设计，不能据此直接切槽。
- **恢复传输**：Linux 可见 `a600000.dwc3`，状态为 `not attached`；USB role 为 `none`，没有报告已绑定 gadget。实际物理恢复口、EDL/fastboot、loader 和进入恢复后的主机可达性仍未验证。

容量按当前 GPT 的 `(last_lba - first_lba + 1) × sector_bytes` 计算。下表只比较官方 `agnos.json` 的 7 类 A/B 更新镜像，共 14 个目标分区，不代表全量工厂刷机布局已兼容。

| 分区（A/B 容量相同） | 每槽实际容量（字节） | 官方 19.9 清单镜像长度（字节） | 容量比较 |
|---|---:|---:|---|
| xbl_a / xbl_b | 3,670,016 | 3,282,256 | 足够 |
| xbl_config_a / xbl_config_b | 364,544 | 98,124 | 足够 |
| abl_a / abl_b | 1,048,576 | 274,432 | 足够 |
| aop_a / aop_b | 524,288 | 184,364 | 足够 |
| devcfg_a / devcfg_b | 131,072 | 40,336 | 足够 |
| boot_a / boot_b | 67,108,864 | 46,962,688 | 足够 |
| system_a / system_b | 12,437,225,472 | 4,718,592,000 | 足够 |

**14 项容量比较通过，仅排除了这些镜像长度大于目标分区的情况。** 没有下载、写入或启动官方系统镜像；不能证明 bootloader、内核、设备树、相机、GPU/DSP 或恢复路径兼容。当前 `/data` 可用 5,377,658,880 字节是另一项观测，不能与原始目标分区容量混用。

## 对照已锁定官方版本

官方 openpilot `35e78a9d9518855ec0dacec919e719624a28131f` 要求 AGNOS **19.9**。第 0 步仅锁定官方系统清单，并未下载其系统分区镜像。本轮确认运行 **10.1**，A 槽 **12.4** 仍为历史证据，既未证明 19.9 能启动，也未证明失败后能恢复。主机编译通过和镜像清单齐全都不能替代这些证据。

## 后续门槛与验收标准

下列门槛是未来更改系统或 MCU 前需要补齐的证据，不是本次只读步骤要执行的操作。

| 门槛 | 尚缺什么 | 达标证据 |
|---|---|---|
| R1 精确当前备份 | 当前 7 个选定文件已与历史相符；完整软件树和当前配置漂移尚未核对 | 未来变更前补存差异，明确精确版本、捕获时间和非原子限制。 |
| R2 主机恢复入口 | 实际物理数据口、供电方案、恢复协议和 loader（如该协议需要） | 板型对应的接口图、工具/loader 来源与哈希、实际协议识别、分区映射和保留数据规则；进入恢复模式及演练需以后另行授权。 |
| R3 A/B 回退 | 当前 GPT/容量和根槽位已核对；配套固件、A 槽内容与启动成功仍缺 | 核实 system_b 与 dsp_a/modem_a 挂载设计；以后的受控测试证明可启动和可返回。共享 /data、persist、systemrw 及 Panda 需独立恢复方案。 |
| R4 GPT | 当前只读 CRC/entries 哈希对照完成；sde 备份 header 的 entry CRC 异常仍在 | 任何未来写入前审查槽位元数据及拟采用 updater/修复的影响。旧候选始终只是离线分析文件；本轮未修复。 |
| R5 Panda/DOS | 每块板实际 MCU、对应镜像、实际 bootstub 和可达恢复方式 | 板型/传输链/固件映射；以后验证正确 MCU 恢复入口及 provisioning 保留方式。不能直接调用会重编译、复位、刷写的通用 recover.py。 |
| R6 完整恢复演练 | 从失败到原配置正常运行的闭环 | 以后单独授权的恢复测试证明启动、原应用与配置、相机/驾驶员监控、声音告警、Panda 识别与通信均恢复。哈希正确不等于恢复通过。 |

## 证据定位

下表路径相对于原始 `/Users/shuang/Code/openpilot` 工作区。它们是出处，不是新工程离线验证的运行依赖；大归档与私有内容未复制。JSON 另记录 13 份镜像的历史哈希与本次 size-only 复核、5 份小固件文件哈希复核、18 组保存 GPT 校验结果。

| 来源 ID | 原始相对路径 | 本次读取报告的 SHA-256 |
|---|---|---|
| `preparation` | `migration-audit/recovery-backup-20261017/preparation-result.json` | `4e2fafb4bf633acaf0d697656d6a49d010fafadadc0cc9480cf733895be1d593` |
| `decision_matrix` | `migration-audit/recovery-backup-20261017/recovery-decision-matrix.json` | `d4692388918dbf99ad24d2c8006cb99043b3625e40589a31522727e86b56f03a` |
| `bundle_verification` | `migration-audit/recovery-backup-20261017/bundle-verification.json` | `ec34222d10756e242683e59a99d55fb0b7595a75b5879cbddce18bd8f59dabed` |
| `configuration_verification` | `migration-audit/recovery-backup-20261017/latest-configuration-verification.json` | `499f0c77b5f975cce4196254e4be2375e55728dfa09a6ac76c1f400781c0d5ad` |
| `gpt_analysis` | `migration-audit/recovery-backup-20261017/gpt/gpt-analysis.json` | `3cad7869251765ef06fecfcdfc8a5bec0a02f8049560c77e1898a005d486e7b0` |
| `gpt_candidate` | `migration-audit/recovery-backup-20261017/gpt/sde-offline-repair-analysis.json` | `7168c2fc44ff2352750ee976ac71e1065c32fb0de5d1b33f6b99c512b4aad347` |
| `panda_manifest` | `migration-audit/recovery-backup-20261017/panda-original/manifest.json` | `36f588403552a23ed55a29ddd6e6bbcc8e598716843d2d97538d0cc12d7c085c` |
| `early_audit` | `migration-audit/rollback/audit-result.json` | `1b70a8511ea39f1c58f5c82e4ea942a8a9f2688c5e6546b6a5e0af16cbe08b64` |
| `slot_images` | `migration-audit/rollback/inactive-slot-verification.json` | `a2590b1cb5894a803f607ba867db1795028b58a0d0a5fc59514dde6774b71673` |
| `slot_layout` | `migration-audit/rollback/device-layout.txt` | `9a2b8a21d52c6cdd4aca949ccc02b8ca6592158c7da4ee2cc22867929f921c8d` |
| `usb_mapping` | `migration-audit/rollback/usb-controller-mapping.txt` | `18156ddd5eee62b02ab613f74e0503b8e100d0dffddcba2ac809c85032554a5a` |
| `old_image_availability` | `migration-audit/rollback/old-image-availability.json` | `aacfd5d41149de5cdf0f78275fec6c93e6605dd91f75e9d2b4e30fe698a4f743` |
| `factory_inspection` | `migration-audit/sp2027-review-20261017/factory-evidence/factory-inspection-summary.json` | `3674d5f77589f63bc20c4de3d3a9a0bf14706e6d923919f2a48fcc08c63ee58e` |
| `research_summary` | `songplus-port-lab/research/evidence/summaries/recovery-backup.json` | `8d7dd5299850580aa8953b0b96ac0970b309ac330f2aac85f06da97527fc9ccf` |
| `official_lock` | `songplus-port-lab/upstream-lock.json` | `8ddb483e54d83a80490671cbcf4cd92a6af93a36e8c7f4b27d053e32eb012947` |

本轮与补充来源：

| 来源 ID | 路径 | SHA-256 |
|---|---|---|
| `live_system` | `hardware/step1/evidence/live-system.json` | `2c81b0ff07859a545d587bed8dde970e64a23af6082dbebf12f5514e9e709fc1` |
| `live_recovery` | `hardware/step1/evidence/live-recovery.json` | `04f47cff0f89158de9ec725c7ebc1cbab53bd32c7922cc22ff3ed6b9057b721e` |
| `historical_software_comparison` | `migration-audit/source-comparison.json` | `f67f6664b3c6cb9c7258fb9176071070d1ab9935e298744ba42cb3a3707870ea` |
