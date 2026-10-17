# 第1步补充：C3XL 产品配置与官方要求

按用户新确认，本机按**无驾驶员相机、无电话卡功能**的 C3XL 配置继续规划。这解释了第三路历史失败的一个合理原因，不再把它默认当作坏相机。具体 SKU、PCB/SOM 修订与批次仍待资料绑定。本补充仅本地阅读主任务采集的证据，旧报告与设备保持不变。用户原始澄清、厂家摘要及本轮被动采集分别保存在同目录的`user-clarification.json`、`vendor-sources.json`、`live-evidence.json`。

厂家 [C3X lite 产品页](https://oneclone.net/product/c3x-lite/)（主任务于2026-10-18读取）明确该系列去掉驾驶员监控和4G，列两路OX03C10、185°/45°视角；现规格表为SD845、8GB RAM、64GB UFS。它支持用户所述产品取舍，但本机MemTotal约3.53GiB、DT内存区域约3.97GiB，与页面8GB不同，不能同名就当同一板型/代际。厂家视角也不是本机标定结果。

| 证据层 | 已知 | 不能据此认定 |
| --- | --- | --- |
| 用户/厂家陈述 | 用户确认本机无驾驶员相机、无电话卡功能；厂家系列说明相互支持 | 页面当前8GB规格就是本机BOM |
| 静态源码/配置 | 旧SP098有缺驾驶员相机时放宽检查的条件；本轮DT启用0/1/2/3四个相机槽位；另一个3xl归档用19.7-c3xl-dev及custom SPI Panda | 四颗物理相机；这些分支对应本机PCB或可直接作为新适配 |
| 软件硬件识别 | SDA845 rev2.1、Adreno630v2、tici标签、DOS消息及USB0600、历史两路OX03C10；2c7c:0125绑定4×option+1×qmi_wwan | 精确PCB/SOM、MCU裸片、可用SIM卡槽或蜂窝业务 |
| 物理证实 | 尚无绑定本机的外观铭牌/板型资料 | 用户需拆机或重新证明产品配置；目前无需这种操作 |

**没有驾驶员相机是官方完整运行的实际缺口。** 锁定官方`35e78a9d`的监控模型等待Cabin图像流，再生成`driverStateV2`；监控进程收到该数据后才发布`driverMonitoringState`。正常实机同时检查Cabin相机和监控消息，相机失活会产生禁止进入/软退出事件。wheel-touch回退仍依赖这条模型消息链，不能当成官方支持“物理无相机”的证据。

因此，现有配置不能原样满足官方完整监控链；这不只是缺一个驱动。旧分支忽略检查的条件代码是差异证据，不是应移植的解决方案。被动协议、解码及离线工程仍可继续；目标完整辅助驾驶运行前，驾驶员监控硬件和可靠图像链需要单独设计、验证。本轮不修改监控策略。

**没有电话卡功能本身不否决迁移。** 官方网络层支持Wi-Fi/Ethernet，也表示无网和SIM缺席；上传无网时等待。Wi-Fi可以承担更新与维护联网。但这不是永久离线承诺：该基线在更新失败且累计驾驶时间超过27小时、行程次数超过84等组合条件下，可以置联网更新告警并影响启动。本轮不调整更新策略。

本轮USB `2c7c:0125`在480Mbps枚举5个接口，**4个绑定`option`、1个绑定`qmi_wwan`**，sysfs有`ttyUSB0..3`、`cdc-wdm0`、`wwan0`。只读取了sysfs元信息，未打开这些设备节点。这证明接口及内核驱动存在，仍不证明卡槽、RF/天线、入网状态或可用蜂窝业务，不能凭此否定“无电话卡功能”。不开串口、不发AT/QMI、不读IMEI/ICCID。

本轮设备树`cam-sensor@0..3`均为`status=ok`，也注册了四个`cam-sensor-driver`节点。这是通用平台的槽位配置和驱动注册，**不是四颗相机的物理探测结果**；不推翻用户确认的无驾驶员相机配置。本轮未启动相机、未探测传感器。

Panda的被动USB枚举为`bbaa:ddcc`、`bcdDevice=0600`。与实机hash匹配的旧API在`panda/python/__init__.py:411–414`取高字节，`164`定义DOS为`0x06`，`176`将DOS归为F4；所以它给既有共享内存里的DOS消息增加了一条软件枚举佐证。USB描述符仍由固件提供，不能当作MCU裸片、PCB修订或固件字节一致性的物理证明。

**外置Panda暂不接到运行中的C3XL。** 当前`pandad`运行，归档SP098的`selfdrive/pandad/pandad.py:24–41,91–109`包含枚举后比对签名、自动刷写和DFU恢复路径。本补充尚未把该包装器hash绑定当前实机，但已足以排除“插上只读查看”的假设；不据此声称本轮发生刷写。连接环境与前提由主任务单列，本轮不新增连接、不停止服务。

最有价值的补充资料只有三类：

1. 该机订单/包装上的具体型号、版本及购买时间/批次；遮住私人订单信息、序列号和二维码。
2. 不拆机的屏幕侧、道路镜头侧、背面外观与端口标识照片；不需新增通电或插线操作。
3. 厂家针对该批次的板型/修订说明、已有PCB照片，以及原厂系统/恢复loader适用清单；不要求用户拆机取证。

本轮已完成白名单soc/DT平台标识、USB接口/驱动和通信节点核查，无需重复枚举。后续优先把厂家批次资料与已有证据对应；确有需要时，可只读既有网卡类型/operstate及`deviceState.networkType`，仍不把它替代蜂窝功能验证。未有字段就保持未知，不启动相机/调制解调器服务、不探测I2C、不改变USB角色，也不连接外置电路板。

源码锚点（均为锁定官方提交）：

- [openpilot/system/camerad/cameras/hw.h:60–73](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/camerad/cameras/hw.h#L60-L73)：Cabin相机作为camera2、PHY2、BPS输入。
- [openpilot/system/manager/process_config.py:12–13](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/manager/process_config.py#L12-L13)：driverview在started或预览时运行。
- [openpilot/system/manager/process_config.py:81–105](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/manager/process_config.py#L81-L105)：正式相机/DM/声音进程。
- [openpilot/selfdrive/modeld/dmonitoringmodeld.py:111–151](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/modeld/dmonitoringmodeld.py#L111-L151)：等待CABIN流并生成driverStateV2。
- [openpilot/selfdrive/monitoring/dmonitoringd.py:12–33](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/monitoring/dmonitoringd.py#L12-L33)：收到driverStateV2才生成driverMonitoringState。
- [openpilot/selfdrive/selfdrived/selfdrived.py:80–98](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/selfdrived/selfdrived.py#L80-L98)：Cabin和DM消息被正常实时检查。
- [openpilot/selfdrive/selfdrived/selfdrived.py:349–361](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/selfdrived/selfdrived.py#L349-L361)：进程与相机消息失活触发故障。
- [openpilot/selfdrive/selfdrived/events.py:632–642](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/selfdrived/events.py#L632-L642)：相机故障/低帧率NO_ENTRY和SOFT_DISABLE。
- [openpilot/selfdrive/selfdrived/selfdrived.py:470–480](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/selfdrived/selfdrived.py#L470-L480)：初始化缺流处理仅涉及道路两流，不去除Cabin要求。
- [openpilot/selfdrive/monitoring/policy.py:301–308](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/selfdrive/monitoring/policy.py#L301-L308)：wheel-touch回退依赖监控模型状态，不是无相机入口。
- [openpilot/common/hardware/comma/hardware.py:123–144](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/hardware.py#L123-L144)：Wi-Fi/Ethernet/cellular/none分别表示。
- [openpilot/common/hardware/comma/hardware.py:146–155](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/hardware.py#L146-L155)：SIM缺席状态有明确表示。
- [openpilot/system/loggerd/uploader.py:245–256](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/loggerd/uploader.py#L245-L256)：无网时上传等待。
- [openpilot/system/updated/updated.py:33–38](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/updated/updated.py#L33-L38)：更新连接时间与路程次数门槛。
- [openpilot/system/updated/updated.py:325–343](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/updated/updated.py#L325-L343)：长时间更新失败可能设置ConnectivityNeeded。
- [openpilot/system/hardware/hardwared.py:352–365](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/system/hardware/hardwared.py#L352-L365)：ConnectivityNeeded进入启动条件。
- [openpilot/common/hardware/comma/hardware.py:34–39](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/hardware.py#L34-L39)：tici名称取自设备树。
- [openpilot/common/hardware/comma/hardware.py:61–70](https://github.com/commaai/openpilot/blob/35e78a9d9518855ec0dacec919e719624a28131f/openpilot/common/hardware/comma/hardware.py#L61-L70)：官方设备族定义。

JSON另保留本轮三份新增证据hash、既有实机证据hash、旧Panda/API与SP098静态源hash及3xl归档来源；这些原归档仅用于溯源，未导入执行。厂家说明属于系列证据，用户陈述、软件字段和物理证实四层不互相替代。
