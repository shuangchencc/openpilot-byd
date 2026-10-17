# 第 1 步：C3XL 只读核查与兼容性缺口

日期：2026-10-18。**本轮只读核查已完成；硬件完整身份、目标版本运行兼容性和恢复能力尚未验收通过。** 官方基线保持 `35e78a9d9518855ec0dacec919e719624a28131f`，没有更新或修改上游。

通过已有 SSH 身份连接用户指定的、离车单独供电设备。采集系统文件、sysfs、限定日志、GPT 元数据和已发布状态；没有写设备文件/参数、安装脚本、启动或停止服务、打开相机、切槽、修复 GPT、发送 CAN、重启或刷写固件。SSH 和设备原有进程自身的正常日志变化不属于采集器写入。

## 实机结果与官方基线对照

| 项目 | 本轮实际证据 | 锁定官方基线及缺口 | 后续验收标准 |
| --- | --- | --- | --- |
| 计算平台 | sysfs：SDA845 rev 2.1、Adreno630v2；设备树 `comma tici`；内核可见内存约 3.53 GiB，无 swap | 平台家族有相应代码，但商业名称或设备树不能确认 PCB/SOM 及外围兼容 | 具体 PCB/SOM、供电与接口资料匹配；实际目标推理耗时、峰值内存、温度和掉帧有记录 |
| 系统与应用环境 | AGNOS 10.1 / Ubuntu 20.04.6 / Linux 4.9.103+，B 槽；系统 Python 3.8.10，应用环境为 Python 3.11.4；NumPy 1.26.4、pycapnp 1.3.0、glibc 2.31；软件声明 0.9.8.0 | 目标 AGNOS 19.9、Python 3.12.13、NumPy 2 系及 pycapnp 2.1.0；旧原生库、kernel/ISP/KGSL ABI 不能直接拼接 | 先形成绑定 boot/ABL/kernel/用户态/GPU/相机依赖的硬件版本清单；未来台架启动和运行验证单独验收 |
| 当前 Panda/DOS | 一个 `bbaa:ddcc` USB 设备，经 a800000 控制器、1-1.2、12 Mbps；两次新鲜 PandaState 均报告唯一 `dos`，`noOutput`、controlsAllowed=false、双 ignition=false、harness=notConnected、无 fault/heartbeatLost | 安装库把 DOS 归为 F4；目标 Panda 只构建 H7，正式 pandad 是单 SPI/单板。`/dev/spidev0.0` 存在不证明这块 DOS 可按目标协议通信 | 明确实际 MCU/PCB/bootstub，分别验证 DOS/F4 支持、USB 传输、多板生命周期和 safety；不能使用第 0 步 H7 产物替代 |
| 外置 Red Panda 与线束 | 本轮未枚举外置 Red Panda；历史车上记录存在 redPanda+DOS | 不能用“本次不在”判断外置板损坏，也不能确定其 MCU/版本或恢复状态；最新正式 pandad 不覆盖旧双 USB 结构 | 未来在板卡实际连接时只读核对身份与固件元信息，并确认物理线束、主机总线与每板本地总线映射 |
| Panda 协议 | 当前安装库哈希与归档一致：health=16、CAN=4、CAN-health=5；固件文件包声明 `DEV-e26a80db-DEBUG` | 新版用结构哈希及新的 health/CAN 布局；文件版本不是直接读取实际 MCU 的固件/bootstub | 新主机与 F4/H7 固件分别有一致的协议、布局及边界测试；不得跳过检查或只改枚举数字 |
| 两路道路相机 | 历史 2026-05-17 原日志：slot 1/0 成功探测 `0x5803`，对应 OX03C10；road/wide 编码初始化 1928×1208。本轮相机处于待机未运行，未取得当前帧 | 新版保留 OX03C10；因此 AR0231 驱动缺失不是这两路已识别道路相机的已知障碍。仍缺镜头、CSI/PHY、ISP ABI、曝光/warp、标定及同步验证 | 原系统自然运行或后续明确安排相机验证时，三路身份/尺寸/时间戳有证据；目标栈再通过帧率、同步、丢帧、图像和标定检查 |
| 驾驶员相机及监控 | 历史同次日志 sensor 2 bringup 失败；当前 DriverCameraHardwareMissing=1，但 mtime 很旧。当前 camerad/modeld/dmonitoringmodeld 均 running=false、shouldBeRunning=false、exitCode=0 | 不能断言当前相机损坏或未装；需区分缺硬件、连接、驱动和配置。新版 cabinCameraState 与旧 driverCameraState 也有接口差异 | 明确实际传感器与物理存在性，正常图像及驾驶员监控可用；保留可听告警，不用禁用监控掩盖缺口 |
| 系统恢复 | 6 盘主/备 header CRC 正常，主备 entries 相同且与历史备份匹配；**sde 备用 header 中的 entries CRC 仍错误**。目标 7 类 A/B 镜像的 14 个分区容量均足够 | 分区容量和备份哈希不能证明可恢复。当前 root 在 B，但 `/dsp`、`/firmware` 分别挂载 dsp_a、modem_a，配套关系必须核对 | 设备专用恢复端口/协议/loader 及精确板型绑定，原始备份、槽位与完整启动链明确；未来实际恢复演练通过。GPT 异常另行审查，当前不修复 |
| 宋 PLUS 控制 | 本轮没有车辆 CAN 输入，也没有实现或测试本车控制 | 配套官方 BYD 只有 ATTO3 且 dashcamOnly；不具备宋 PLUS 直接控制支持 | 本车被动数据、只读 CarState、控制器与专用 safety 按独立里程碑验收 |

## 恢复材料：已经具备和仍然缺少

已有 13 份块镜像、7,503 项软件归档和 182 项配置的历史校验记录。本轮重新核对了镜像存在/大小、选定小文件哈希和当前 GPT 元数据；没有重新读取数十 GB 镜像，也没有做恢复演练。

当前 5 个 Panda 安装文件及 build.json、camerad 与历史记录匹配。它们证明安装文件一致，不证明 MCU 中的 bootstub/provisioning 完整，也不证明相机当前运行。

A 槽 AGNOS 12.4 仍是此前离线镜像分析结果；本轮没有挂载或启动它。EDL/Fastboot 实际入口、设备适配 loader、可独立恢复的端口和回滚过程均未验证。具体来源、14 项容量计算与门槛见 [恢复条件报告](recovery-prerequisites.md)。

## 证据及方法

- [实机证据清单](evidence-manifest.json)：封装本轮结构化结果与白名单摘要，记录 SHA-256；不含密钥、IP、序列号、图像、原始配置或完整日志。
- [官方源码要求与逐项对照](official-requirements.md)：精确提交、文件、行号和哈希；保留条件推论与未知项。
- [机器可读结论](STEP-1-RESULT.json)：本轮完成范围、未过门槛及验收记录。
- `tools/inspect_c3xl_readonly.py`：只有显式 `--connect` 才连接；其他调用仅预览。不会被 bootstrap 或普通离线验证调用。
- `tools/decode_c3xl_states.py`：仅在开发机用哈希匹配的旧 schema 解析已复制消息；不导入旧设备业务代码。

现有状态消息使用 **O_RDONLY/pread 读取已存在共享内存**，没有调用 Panda 构造器或注册 IPC reader。每个服务保留两个写入指针一致的快照，在本机确认消息时间戳前进、valid=true、采样时延不超过 2 秒。原始帧只在被忽略的 results/；提交内容只保留白名单字段。

早期一次快照因高频发布竞争未取得 Panda 帧，未被当作状态证据；修正采样后取得完整新鲜样本，失败尝试仍保留在本地 results/。没有为获取状态退回到会创建/截断 IPC 文件的订阅入口。

本地复验：`python3 hardware/step1/verify.py`；采样算法回归：`python3 -m unittest discover -s hardware/step1/tests -v`。二者不连接设备。原第 0 步构建结果继续保留，本轮不把它重新记作实机通过。

## 下一步顺序

1. 先补物理板卡/端口和恢复链证据，尤其外置 Red Panda、DOS 实际 MCU、驾驶员相机与恢复入口。这些未知不能靠更换版本号消除。
2. 可先在本地设计 DOS/F4 + USB/双 Panda 的硬件支持层及新旧协议迁移，保持与车型控制工作分离；不生成可直接刷写的发布包。
3. 相机链、配套系统和恢复条件明确后，再单独安排目标机台架运行；期间仍不接入车辆控制。
