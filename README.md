# 宋 PLUS 独立维护工程

目标：保留现有 C3XL 和 Panda，为 **2023 宋 PLUS DM-i 冠军版 110 km 旗舰 PLUS** 建立基于官方配套栈的适配。

当前完成第 0 步：固定官方源码、配套依赖和模型，整理可迁移的研究证据，建立离线构建与验收入口。尚未实现宋车型端口，也未确认 C3XL 和现有 Panda 能运行这套版本。工程不依赖 Mr One 加密车型模块。

第 1 步已完成授权的只读实机核查及补证，最新见 [板型、产品配置与恢复链](hardware/step1/followup/RESULT.md) 和 [待补材料清单](hardware/step1/followup/USER-CHECKLIST.md)，此前 [实机快照](hardware/step1/STEP-1-RESULT.md) 保留。已确认 SDA845/Adreno630、AGNOS 10.1 B 槽和内部 DOS 软件身份；用户确认本机出厂无驾驶员相机、无电话卡功能，厂家系列资料支持该取舍。具体 PCB/MCU、外置 Panda、道路相机当前运行及恢复能力仍有未过门槛，官方驾驶员监控硬件差异需单独解决。所有设备写入操作继续保持关闭。

DOS/F4、USB 双 Panda 兼容层的 **P0/P1 已完成纯本地实现和离线验收**，见 [结果与范围](validation/panda-compat-p0p1/RESULT.md) 及 [独立补丁维护说明](patches/openpilot/p0p1/README.md)。实现包含严格未绑定profile、身份/角色注册、模拟传输、C++ CAN和Python版本/health编解码；尚未接入真实pandad、USB或固件。固定基线及原始上游工作树保持不变。

用户无法提供照片和厂家资料，实物未知项继续保留，不阻断本地工作。[兼容层设计](hardware/design/dos-usb-dual-panda/DESIGN.md) 与其NOT_RUN清单是实现前冻结的历史快照；P0/P1的新验收结果以上述报告为准，P2–P7仍待实施。

## 固定基线

“最新版”指 **2026-10-18 00:54:34 +08:00 核验的官方 master**，之后不会自动跟随 HEAD。最新发布版另记为 `v0.11.1`，不把 master 称为稳定发行版。

| 部件 | 固定版本 |
| --- | --- |
| openpilot | `35e78a9d9518855ec0dacec919e719624a28131f` |
| opendbc（目标栈） | `b3bd79a659dae961af169a3baac723961f720133` |
| panda | `a59195163c32a3a375ea6c86715dc08fd0984c1b` |
| tinygrad | `02e698160eed1751738212144a7ea2cfb160b3f1` |
| Python / uv | `3.12.13` / `0.13.1` |
| AGNOS | `19.9`，只锁定官方 manifest，未下载或安装系统分区镜像 |
| 开发机 LLVM / zstd | `21.1.8_1` / `1.5.7_1`，只安装在本项目 `.bootstrap/` |

[upstream-lock.json](upstream-lock.json) 固定完整递归提交、依赖锁、253 个 LFS 实体及 AGNOS manifests；253 个实体共 922,030,767 字节，含 5 个模型文件。[host-toolchain-lock.json](host-toolchain-lock.json) 固定额外的 macOS 编译库及其来源和哈希。

历史研究继续使用独立的 `upstream-opendbc/`：`06ade56b49162e3f35e354df115e97b02de81d7a`、Python `3.12.7`。它不是目标栈的 opendbc，不用它替换目标子模块。

## 目录和入口

| 路径 | 用途 |
| --- | --- |
| `upstream-openpilot/` | 官方完整源码基线；递归子模块均固定，受跟踪源码保持原样 |
| `upstream-opendbc/`、`baseline-lock.json` | 历史候选解码与 safety 参考基线 |
| `research/INDEX.txt`、`research/index.json` | 10 项历史成果的来源、完成程度和限制 |
| `research/evidence/`、`research/plans/` | 41 份证据材料、6 份历史采样计划快照 |
| `hardware/` | 硬件差异、31 个来源、44 个锚点、9 项待实施补丁 |
| `spec/`、`decoding/`、`replay/` | 协议证据要求、只读候选解码、严格日志输入校验 |
| `tools/bootstrap_host.sh` | 显式联网准备，安装范围限定在项目中 |
| `tools/validate_project.py` | 第 0 步完整本地验收入口 |
| `tools/validate_reference.py` | 历史研究工具与参考版测试入口 |
| `tools/validate_upstream.py` | 目标版本的 macOS minimal 构建与精选官方测试 |
| `validation/` | 本次可追溯的验收摘要；详细日志在忽略的 `results/` |
| `MAINTENANCE.txt`、`project.json` | 更新规则、工程范围和后续阶段 |

同目录另有任务维护的 `capture-plan/`、旧 `README.txt` 和 `validate.py`，它们不属于本次提交，也不是本工程验收依赖。第 0 步入口以上表为准；历史采样方案已有独立快照。原始备份、配置、密钥、受保护载荷及完整恢复实验保留在原归档，不迁入此仓库。

## 准备与离线复验

完整主机构建目前限定 **Apple Silicon、macOS 26.0+、完整 Xcode**；本次验证使用 Xcode 26.6 / Apple clang 21。需要 Git 和 Python 3.11+ 执行准备脚本。Xcode 本身是系统先决条件，不由本工程下载安装。Python 环境、官方依赖、LFS 和 LLVM/zstd 均在项目内准备；不安装全局 Homebrew 包。

首次克隆或显式补齐依赖时，在仓库根目录执行（需要网络）：

```sh
bash tools/bootstrap_host.sh
```

准备完成后执行（不下载依赖）：

```sh
python3 tools/validate_project.py
```

主机编译、测试和已安装依赖一致性检查在 macOS sandbox 中禁止远程出站网络，允许本机 IPC。验收会检查源码、Git links、LFS 实体、环境与锁文件的一致性；缺失或漂移直接失败。结果写入 `results/project/validation-result.json`，退出码 0 仅表示所声明的本地范围通过。

仅验证源码和证据，可执行 `python3 tools/validate_project.py --sources-only`；它不算构建通过。源码 CI 使用 `--allow-lfs-pointers`，仅确认模型指针与固定对象一致；本机完整验收要求真实 LFS 文件。`validate_upstream.py --core-only` 会省去模型编译，也不算完整 minimal 构建。

`.github/workflows/source-and-research.yml` 已准备源码/证据/历史研究回归，Actions 本身按提交固定；目前未推送、未在 GitHub 执行。CI 不覆盖完整 Metal 模型编译。它的联网准备使用 `bootstrap_host.sh --reference-only`，不会下载目标模型或 LLVM。

## 验收结论和边界

本次详细结论见 [validation/STEP-0-RESULT.md](validation/STEP-0-RESULT.md)。已完成官方 macOS `scons --minimal` 构建，包括 Metal 驾驶/监控模型和 H7 固件本地交叉编译；精选目标版本测试执行 130 项，119 项通过、11 项跳过。

官方 Darwin 构建不覆盖 Linux/AGNOS 的 `camerad`、`pandad`，也不证明 C3XL 的相机、QCOM 推理后端或实时性能可用。本地 H7 固件为官方默认开发构建（debug 签名、`ALLOW_DEBUG`）；尚未确认实物板型。若现有 DOS 使用 F4，当前 H7-only 上游构建不能覆盖它。

硬件 9 项补丁仍未实施；本车已确认协议条目仍为 0；宋车型 CarState、controller 与专用 safety 尚未完成。已有 checksum、helper、builder 和控制器恢复研究按原报告范围整理，不作为整车控制验收，也不成为构建依赖。

第 0 步未连接 C3XL；第 1 步使用独立的显式只读 SSH 入口。两步均未发送 CAN、启动设备服务、修改设备系统或刷 Panda。设备地址只保存在被忽略的 `local/` 中。

第 1 步证据可用 `python3 hardware/step1/verify.py` 离线复核。诊断工具默认只预览，`--connect` 才使用本地连接配置；不会混入 bootstrap、CI 或通常的离线验证入口。

第 1 步补充证据另用 `python3 hardware/step1/followup/verify.py` 离线复核；采集入口 `tools/inspect_c3xl_followup.py` 同样默认不连接。此前外置 Panda 身份与供电资料请求保留为历史未知项；本轮不接回运行中的 C3XL 做枚举，继续推进本地设计。

兼容层设计包可用 `python3 hardware/design/dos-usb-dual-panda/verify.py` 离线复核。此检查只验证设计文件、源码锚点、阶段依赖和验收覆盖，不运行Panda代码、不表示实现测试通过。此前照片/资料清单保留为历史待补项，当前本地工作不等待这些材料。

后续先核实板卡/相机/线束与恢复能力，同时利用带来源和事件标注的被动日志确认本车协议。随后实现只读 CarState，再推进控制器、匹配 safety、目标机台架与受控车辆验收。验收标准和顺序见 [NEXT-STEPS.txt](NEXT-STEPS.txt) 及 [hardware/patch-plan.txt](hardware/patch-plan.txt)。
