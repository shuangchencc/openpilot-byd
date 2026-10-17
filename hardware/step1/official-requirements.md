# 第1步：官方要求与当前 C3XL 对照

官方基线保持不变：openpilot `35e78a9d9518855ec0dacec919e719624a28131f`；panda `a59195163c32a3a375ea6c86715dc08fd0984c1b`；opendbc `b3bd79a659dae961af169a3baac723961f720133`；tinygrad `02e698160eed1751738212144a7ea2cfb160b3f1`。

本表审查本地锁定源码，并对照主任务2026-10-18（上海时间）的只读采集。本文作者没有另行连接设备。已有状态消息采用O_RDONLY共享内存快照，在本机按匹配旧schema解码；没有注册IPC reader。历史相机日志与本轮状态明确分开。

当前明确差异是旧系统/Python依赖、当前DOS/F4支持路径与目标H7-only、USB/新单SPI主进程架构、协议代际。SDA845/Adreno630属于有利的家族匹配；两路道路OX03C10已有历史正向识别，不能把缺AR0231驱动列为这两路的已证实障碍。

## R01 硬件身份与平台选择

**源码事实：** 官方以 /AGNOS 存在选择 HardwareComma；设备类型来自 device-tree model 去掉 comma 前缀。类注释覆盖 Snapdragon 845 的 tici/comma three、tizi/comma 3X、mici/comma four。

**本轮/历史证据：** 本轮sysfs: SDA845、revision2.1、soc_id341；GPU=Adreno630v2；device-tree=comma tici。；MemTotal=3699784kB；无swap。

**对照结论：** 处理器/GPU家族与官方845/a630路径相符；PCB/SOM/BOM/引脚仍未由软件字段完整识别。

**尚缺：** PCB/SOM revision和外设连接；内存容量只能区分内核可见值与物理BOM。

**设备证据：** `hardware/step1/evidence/live-system.json`, `hardware/step1/evidence/live-details.json`。

**锁定源码锚点：**

- [commaai/openpilot · openpilot/common/hardware/__init__.py:8–16](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/__init__.py#L8-L16)：平台检测。
- [commaai/openpilot · openpilot/common/hardware/comma/hardware.py:34–39](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/hardware.py#L34-L39)：设备名称来源。
- [commaai/openpilot · openpilot/common/hardware/comma/hardware.py:61–70](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/hardware.py#L61-L70)：官方平台定义。

## R02 AGNOS / Python 与用户态依赖

**源码事实：** launch_env.sh 默认目标 AGNOS 19.9；.python-version 固定 3.12.13；pyproject 允许 >=3.12.3,<3.13，并要求 numpy>=2、pycapnp2.1.0 及 comma-deps 原生包。

**本轮/历史证据：** 本轮AGNOS10.1、Ubuntu20.04.6、kernel4.9.103+、glibc2.31。；系统python3=3.8.10；已有应用进程解释器=3.11.4；包元数据NumPy1.26.4、pycapnp1.3.0、ONNXRuntime1.17.0。

**对照结论：** 相对目标19.9/Python3.12.13/NumPy>=2/pycapnp2.1.0存在明确用户态版本差异；旧应用环境不能直接作为新基线环境。

**尚缺：** 目标AGNOS可启动性；新版Python原生包及驱动ABI配套；dpkg-query失败未修复，不能据此判断依赖不存在。

**设备证据：** `hardware/step1/evidence/live-system.json`, `hardware/step1/evidence/live-details.json`。

**锁定源码锚点：**

- [commaai/openpilot · launch_env.sh:18–20](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/launch_env.sh#L18-L20)：默认系统版本。
- [commaai/openpilot · .python-version:1–1](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/.python-version#L1-L1)：精确 Python pin。
- [commaai/openpilot · pyproject.toml:1–36](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/pyproject.toml#L1-L36)：Python 范围和主要依赖。

## R03 三路相机 sensor 驱动

**源码事实：** 正式 camerad 仅编译 ox03c10.cc、os04c10.cc；openSensor 依次探测 OS04C10、OX03C10，失败禁用该相机。camera.py 保留 AR0231 几何映射，但不构成 AR0231 实时驱动。

**本轮/历史证据：** 历史kernel日志slot1/slave0x20和slot0/slave0x6c的成功sensor_id均0x5803，对应OX03C10；同次道路/广角编码器初始化1928x1208。；本轮内核日志未见新的相机probe；未主动启动相机。

**对照结论：** 两路道路sensor已有历史正向识别且在目标驱动集合内；AR0231缺失不是已识别道路相机的已证实障碍。当前运行、镜头、同步及第三路仍待核实。

**尚缺：** 当前三路采集状态；驾驶员相机实物存在/接线/驱动；帧格式、内参、目标ISP ABI。

**设备证据：** `hardware/step1/evidence/historical-camera-summary.json`, `hardware/step1/evidence/live-system.json`。

**锁定源码锚点：**

- [commaai/openpilot · openpilot/system/camerad/SConscript:5–8](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/camerad/SConscript#L5-L8)：编译 sensor 集。
- [commaai/openpilot · openpilot/system/camerad/cameras/spectra.cc:1036–1055](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/camerad/cameras/spectra.cc#L1036-L1055)：运行时探测集。
- [commaai/openpilot · openpilot/common/transformations/camera.py:49–71](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/transformations/camera.py#L49-L71)：历史几何映射边界。

## R04 相机端口、内核 ISP ABI 与监控链

**源码事实：** 官方默认 camera_num0=wide/PHY0、1=narrow/PHY1、2=cabin/PHY2；cabin 用 BPS 且 staggered_sof=true。Spectra 需要 req-mgr/cam_sync、cam-isp、cam-icp 设备与相应 ioctls。

**本轮/历史证据：** 本轮存在V4L设备节点；存在节点不证明当前相机工作。；DriverCameraHardwareMissing=1，但mtime是2026-01-14的遗留值；历史日志sensor2曾失败。；既有共享内存managerState新鲜快照：camerad/modeld/dmonitoringmodeld均running=false、shouldBeRunning=false、exitCode=0；deviceState.started=false。

**对照结论：** 本轮为离车待机，相机/模型按当前管理器预期未运行，不能判为本轮相机故障；遗留缺失参数和历史sensor2失败仍需后续针对驾驶员相机核实。未验证目标三路/DM/告警。

**尚缺：** 目标三路帧率/时间戳/ISP；DM与可听告警运行验证。

**设备证据：** `hardware/step1/evidence/live-system.json`, `hardware/step1/evidence/live-details.json`, `hardware/step1/evidence/historical-camera-summary.json`, `hardware/step1/evidence/live-messages.json`。

**锁定源码锚点：**

- [commaai/openpilot · openpilot/system/camerad/cameras/hw.h:34–73](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/camerad/cameras/hw.h#L34-L73)：默认三路配置。
- [commaai/openpilot · openpilot/system/camerad/cameras/spectra.cc:177–217](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/camerad/cameras/spectra.cc#L177-L217)：驱动节点与 ioctl 接口。
- [commaai/openpilot · openpilot/system/manager/process_config.py:81–107](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/manager/process_config.py#L81-L107)：相机、DM、soundd 进程。

## R05 Panda MCU / DOS 板支持

**源码事实：** 配套 panda 的 McuType 只有 H7；正式 MCU 构建仅 panda_h7/body_h7。Python supported devices 为 RED_PANDA、TRES、CUATRO、BODY；内部设备仅 TRES/CUATRO；H7 板探测不含 DOS。

**本轮/历史证据：** 本轮USB枚举仅1个bbaa:ddcc，内部a800000/1-1.2，speed12Mbps。；匹配实机SHA的旧Panda Python源码把DOS类型0x06列入F4_DEVICES。；安装树同时存在F4/H7固件与bootstub文件，版本文件DEV-e26a80db-DEBUG；这些文件不等于直接读出的当前MCU固件。；既有共享内存pandaStates新鲜快照：唯一板型dos，noOutput，controlsAllowed=false；harness=notConnected，ignitionLine/ignitionCan=false，heartbeatLost=false，faultStatus=none。

**对照结论：** 当前DOS板型由已有服务的新鲜状态确认；对应实机旧API归为F4，而目标已无DOS/F4支持，因此板支持差异已成立。未读MCU裸片标识/PCB revision，外置red本轮缺席不能据此判故障。

**尚缺：** 实际MCU精确型号/PCB revision；外置red未接时无法验收其当前板状态；实际固件签名/bootstub/provisioning。

**设备证据：** `hardware/step1/evidence/live-system.json`, `hardware/step1/evidence/live-recovery.json`, `hardware/step1/evidence/installed-panda-api-summary.json`, `hardware/step1/evidence/live-messages.json`。

**锁定源码锚点：**

- [commaai/panda · python/constants.py:34–57](https://github.com/commaai/panda/blob/a59195163c32a3a375ea6c86715dc08fd0984c1b/python/constants.py#L34-L57)：H7-only MCU enum/config。
- [commaai/panda · SConscript:129–171](https://github.com/commaai/panda/blob/a59195163c32a3a375ea6c86715dc08fd0984c1b/SConscript#L129-L171)：H7-only build targets。
- [commaai/panda · python/__init__.py:130–155](https://github.com/commaai/panda/blob/a59195163c32a3a375ea6c86715dc08fd0984c1b/python/__init__.py#L130-L155)：支持及内部 board IDs。
- [commaai/panda · board/stm32h7/board.h:18–51](https://github.com/commaai/panda/blob/a59195163c32a3a375ea6c86715dc08fd0984c1b/board/stm32h7/board.h#L18-L51)：H7 board 选择。

## R06 正式 pandad 已改单 SPI / 单 Panda

**源码事实：** 当前 C++ Panda 只持有 PandaSpiHandle；构造/list 都走 SPI。正式 pandad 仅编译 panda.cc+spi.cc，发布单个 PandaState；发送端忽略 src>=4。SPI 节点为 /dev/spidev0.0，设定50MHz，源码提示部分旧 comma three 不支持此速率。Python Panda 保留 USB 连接不代表正式 pandad 支持 USB。

**本轮/历史证据：** 当前只有a800000内部USB Panda；a600000角色none、UDC not attached、gadget未绑定。；/dev/spidev0.0存在，未主动探测其通信。；新鲜managerState确认旧pandad运行，pandaStates只报告一个DOS；目前没有第二块板的运行样本。

**对照结论：** 当前运行的旧USB DOS路径与目标正式单SPI pandad存在明确架构差异。保留原DOS+red双板设计还须实现USB/双板生命周期和总线安全映射；本轮只验证了一个DOS状态，没有验证外置red。

**尚缺：** 外板/线束连接状态；端口与物理板映射；后续USB/双板安全路由实现。

**设备证据：** `hardware/step1/evidence/live-system.json`, `hardware/step1/evidence/live-details.json`, `hardware/step1/evidence/live-messages.json`。

**锁定源码锚点：**

- [commaai/openpilot · openpilot/selfdrive/pandad/panda.h:46–53](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/pandad/panda.h#L46-L53)：SPI handle。
- [commaai/openpilot · openpilot/selfdrive/pandad/panda.cc:15–36](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/pandad/panda.cc#L15-L36)：SPI constructor/list。
- [commaai/openpilot · openpilot/selfdrive/pandad/panda.cc:168–178](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/pandad/panda.cc#L168-L178)：src>=4被忽略。
- [commaai/openpilot · openpilot/selfdrive/pandad/pandad.cc:166–170](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/pandad/pandad.cc#L166-L170)：单个 PandaState。
- [commaai/openpilot · openpilot/selfdrive/pandad/SConscript:3–7](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/pandad/SConscript#L3-L7)：正式编译传输。
- [commaai/openpilot · openpilot/selfdrive/pandad/spi.cc:39–90](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/pandad/spi.cc#L39-L90)：节点/50MHz/副作用。

## R07 health / CAN 协议契约

**源码事实：** 新 Panda 协议版本是结构文件 SHA256 前4字节按 little-endian 转出的 uint32，而非旧顺序小整数；0xdd 返回8字节 <II>。本锁定版本 health=0xffbc12f5，CAN=0x75abf276；health_t 采用 packed flags 及新的字段布局。

**本轮/历史证据：** 与本轮实机文件SHA一致的旧API常数health16/CAN4/CAN-health5；新基线为结构hash uint32，0xdd响应8字节。

**对照结论：** 主机协议代际差异已确认，须连同firmware/safety整体迁移；本轮未读取MCU0xdd，因此不把静态常数当成当前硬件响应。

**尚缺：** MCU实际包版本/签名的受审查元信息读取；新协议和safety在DOS/F4的构建/测试；SPI/USB实现选择。

**设备证据：** `hardware/step1/evidence/live-recovery.json`, `hardware/step1/evidence/installed-panda-api-summary.json`。

**锁定源码锚点：**

- [commaai/panda · SConscript:159–165](https://github.com/commaai/panda/blob/a59195163c32a3a375ea6c86715dc08fd0984c1b/SConscript#L159-L165)：结构hash版本生成。
- [commaai/panda · python/__init__.py:137–140](https://github.com/commaai/panda/blob/a59195163c32a3a375ea6c86715dc08fd0984c1b/python/__init__.py#L137-L140)：host结构契约。
- [commaai/panda · python/__init__.py:617–621](https://github.com/commaai/panda/blob/a59195163c32a3a375ea6c86715dc08fd0984c1b/python/__init__.py#L617-L621)：8字节版本读取。
- [commaai/panda · board/main_comms.h:226–231](https://github.com/commaai/panda/blob/a59195163c32a3a375ea6c86715dc08fd0984c1b/board/main_comms.h#L226-L231)：MCU响应。
- [commaai/panda · board/health.h:13–35](https://github.com/commaai/panda/blob/a59195163c32a3a375ea6c86715dc08fd0984c1b/board/health.h#L13-L35)：health_t 布局。

## R08 QCOM 推理与模型构建

**源码事实：** comma_arm64 构建使用 QCOM/IMAGE/FLOAT16，编译 driving_supercombo.onnx 和 dmonitoring_model.onnx；普通 AGNOS 运行时选 QCOM。匹配 tinygrad 直接打开 /dev/kgsl-3d0，创建context并设置功耗约束；QCOMCL 编译器要求 a630，加载 llvm-qcom 动态库。

**本轮/历史证据：** 本轮GPU报告Adreno630v2、SoC为SDA845；/dev/kgsl-3d0存在。；可见内存3699784kB，采集时MemAvailable2504232kB；当前为旧用户态。；新鲜managerState确认modeld/dmonitoringmodeld未运行，deviceState.started=false；本轮观察的是待机状态。

**对照结论：** a630编译器家族条件有正向证据；不证明新版KGSL/llvm-qcom/模型实时性。没有运行新模型、压力测试或QCOM设备构造。

**尚缺：** llvm-qcom库/ABI；目标QCOM正确性/时延/内存；长时散热、帧同步和并发DM。

**设备证据：** `hardware/step1/evidence/live-system.json`, `hardware/step1/evidence/live-details.json`, `hardware/step1/evidence/live-messages.json`。

**锁定源码锚点：**

- [commaai/openpilot · openpilot/selfdrive/modeld/SConscript:25–52](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/modeld/SConscript#L25-L52)：QCOM构建和模型配对。
- [commaai/openpilot · openpilot/selfdrive/modeld/helpers.py:15–20](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/modeld/helpers.py#L15-L20)：AGNOS运行后端。
- [tinygrad/tinygrad · tinygrad/runtime/ops_qcom.py:311–335](https://github.com/tinygrad/tinygrad/blob/02e698160eed1751738212144a7ea2cfb160b3f1/tinygrad/runtime/ops_qcom.py#L311-L335)：KGSL ABI及初始化写操作。
- [tinygrad/tinygrad · tinygrad/runtime/support/compiler_qcom.py:10–20](https://github.com/tinygrad/tinygrad/blob/02e698160eed1751738212144a7ea2cfb160b3f1/tinygrad/runtime/support/compiler_qcom.py#L10-L20)：a630编译要求。
- [tinygrad/tinygrad · tinygrad/runtime/autogen/llvm_qcom.py:3–10](https://github.com/tinygrad/tinygrad/blob/02e698160eed1751738212144a7ea2cfb160b3f1/tinygrad/runtime/autogen/llvm_qcom.py#L3-L10)：llvm-qcom库。

## R09 启动入口不属于只读诊断

**源码事实：** 官方 launcher 会改权限、标记slot成功、检查OS并执行updater；pandad.py 会reset内部Panda，必要时DFU恢复并flash。Panda构造/connect会禁heartbeat检查、power-save、reset通信、CAN-FD、CAN速度写入。agnos.py --verify 在校验成功后调用swap并修改slot。

**本轮/历史证据：** 本轮证据method声明OS/file读取与O_RDONLY GPT读取；目标上游入口仅作源码阅读。；本轮已有消息采用O_RDONLY+pread共享内存快照，在本机按匹配旧schema解析；每服务2帧新鲜且单调时间前进，不注册reader、不更新读指针、不导入车机IPC包。

**对照结论：** 实际采集将读取入口与启动/更新入口分离；共享内存只读快照没有借用会写reader状态的订阅器。本项不是新系统或Panda运行兼容性通过。

**尚缺：** 最终命令清单复核；后续主动运行与刷写独立授权。

**设备证据：** `hardware/step1/evidence/live-system.json`, `hardware/step1/evidence/live-recovery.json`, `hardware/step1/evidence/live-messages.json`。

**锁定源码锚点：**

- [commaai/openpilot · launch_chffrplus.sh:7–30](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/launch_chffrplus.sh#L7-L30)：launcher修改与OS更新。
- [commaai/openpilot · openpilot/selfdrive/pandad/pandad.py:35–45](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/pandad/pandad.py#L35-L45)：恢复/刷写。
- [commaai/openpilot · openpilot/selfdrive/pandad/pandad.py:73–86](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/pandad/pandad.py#L73-L86)：启动reset/flash路径。
- [commaai/panda · python/__init__.py:212–247](https://github.com/commaai/panda/blob/a59195163c32a3a375ea6c86715dc08fd0984c1b/python/__init__.py#L212-L247)：构造连接写入。
- [commaai/openpilot · openpilot/common/hardware/comma/agnos.py:210–217](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/agnos.py#L210-L217)：swap写slot。
- [commaai/openpilot · openpilot/common/hardware/comma/agnos.py:263–277](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/agnos.py#L263-L277)：--verify会swap。

## R10 恢复条件与系统分区

**源码事实：** 目标AGNOS manifest包含A/B的xbl、xbl_config、abl、aop、devcfg、boot、system；system原始尺寸4,718,592,000字节。更新器写目标slot并使用abctl，不是只替换用户态。manifest不提供C3XL专属恢复loader、PCB支持证明或回滚演练。

**本轮/历史证据：** 本轮boot参数为B槽/root=/dev/sda7，root与persist挂载只读。；目标7种分区A/B均存在且容量足以容纳目标manifest；boot各67108864字节，system各12437225472字节。；sde主备条目数据相同，主表CRC有效，备用GPT表项CRC无效；未修复。；安装树含F4/H7固件及bootstub，未确认读回签名、loader、provisioning或恢复演练。；当前根系统为B，但/dsp挂载sde9（dsp_a）、/firmware挂载sde4（modem_a）；这是本轮实际旧系统布局，不自动等于故障。

**对照结论：** 分区容量满足是静态正向条件；启动兼容、独立恢复链与可靠回滚仍未通过。GPT异常为本轮再次观察到的阻碍，不能仅因条目数据相同忽略。

**尚缺：** 备份与当前设备绑定/完整性；适用本板loader和恢复入口；可恢复供电/USB路径；GPT异常分析方案；非活动slot启动/恢复演练另行授权；新版根系统与DSP/modem分区的实际组合及ABI配套。

**设备证据：** `hardware/step1/evidence/live-system.json`, `hardware/step1/evidence/live-recovery.json`。

**锁定源码锚点：**

- [commaai/openpilot · openpilot/common/hardware/comma/agnos.json:1–83](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/agnos.json#L1-L83)：七分区目标清单。
- [commaai/openpilot · openpilot/common/hardware/comma/agnos.py:94–110](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/agnos.py#L94-L110)：目标slot/分区路径。
- [commaai/openpilot · openpilot/common/hardware/comma/agnos.py:225–249](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/agnos.py#L225-L249)：更新器写slot。

## R11 宋 PLUS 车型与 safety 仍缺失

**源码事实：** 配套opendbc BYD平台仅ATTO3；interface设置dashcamOnly=true、alphaLongitudinalAvailable=false。BYD safety enum=35，注册在ALLOW_DEBUG中；参数param未用，转向参数注明ATTO3，TX仅0x1E2/0x316/0x3B0。

**本轮/历史证据：** 本轮安装树COMMA_VERSION=0.9.8.0且无.git元数据。；本轮是离车硬件/系统只读检查，未获得新本车CAN协议验证。

**对照结论：** 宋PLUS端口与独立safety仍缺；硬件核实或目标主机构建不会消除车型层缺口。

**尚缺：** 本车日志协议证据；车型端口/controller/safety；板/总线/安全配置的端到端验证。

**设备证据：** `hardware/step1/evidence/live-recovery.json`。

**锁定源码锚点：**

- [commaai/opendbc · opendbc/car/byd/values.py:40–57](https://github.com/commaai/opendbc/blob/b3bd79a659dae961af169a3baac723961f720133/opendbc/car/byd/values.py#L40-L57)：ATTO3平台。
- [commaai/opendbc · opendbc/car/byd/interface.py:12–26](https://github.com/commaai/opendbc/blob/b3bd79a659dae961af169a3baac723961f720133/opendbc/car/byd/interface.py#L12-L26)：dashcam及纵向状态。
- [commaai/opendbc · opendbc/car/car.capnp:647–647](https://github.com/commaai/opendbc/blob/b3bd79a659dae961af169a3baac723961f720133/opendbc/car/car.capnp#L647-L647)：BYD enum35。
- [commaai/opendbc · opendbc/safety/safety.h:413–422](https://github.com/commaai/opendbc/blob/b3bd79a659dae961af169a3baac723961f720133/opendbc/safety/safety.h#L413-L422)：debug-only注册。
- [commaai/opendbc · opendbc/safety/modes/byd.h:62–67](https://github.com/commaai/opendbc/blob/b3bd79a659dae961af169a3baac723961f720133/opendbc/safety/modes/byd.h#L62-L67)：ATTO3参数。
- [commaai/opendbc · opendbc/safety/modes/byd.h:93–109](https://github.com/commaai/opendbc/blob/b3bd79a659dae961af169a3baac723961f720133/opendbc/safety/modes/byd.h#L93-L109)：param忽略/TX RX约束。

JSON 同时保存每个源码文件的 SHA256、提交、行号，以及事实/推论/未知的分层。硬件、目标运行兼容性和恢复演练分别验收；主机构建通过不能替代这些条件。
