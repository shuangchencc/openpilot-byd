# DOS/F4 与 USB 双 Panda：主机兼容层设计

状态：**仅设计，未实现、未构建、未执行验收**。本轮没有连接设备、修改源码/子模块或刷固件。官方目标仍为 openpilot `35e78a9d9518855ec0dacec919e719624a28131f`，配套 panda `a59195163c32a3a375ea6c86715dc08fd0984c1b`、opendbc `b3bd79a659dae961af169a3baac723961f720133`。DP只作静态参考，不替换锁定基线。

选择：未来目标固件的F4/H7统一当前协议与safety；旧16/4/5协议仅作离线解析。v1保留现有cereal编码，固定两个required slot，任何板异常整组不可用于控制；不自动重映射、不单板降级。新wrapper默认不reset/flash/recover。下面的profile/接口都是设计模板，尚无runtime consumer；SEND gate只在未来离线测试中模拟。

## 源码事实

- **F01** 锁定官方C++运行链是单SPI板，不是可直接换USB参数的双板驱动；PythonUSB能力、旧panda.bin.signed文件名、DOS schema枚举/测试均不构成运行支持。 来源：O01, O02, O04, O06, O07, O08, O11, O16, O18, P04。
- **F02** SP098包装器内部板排最后；DP内部板排最先。DP的peripheral[0]及DOS+red点火特例依赖它自己的排序；不能把二者拼接，也不能把本机软件DOS自动绑成slot0。SP098 C++源未见，仅以现有Python排序事实作比较。 来源：L01, R01, R03。
- **F03** 目标health和版本协议已变化：旧API声明health16/CAN4/CANhealth5与3字节回复，目标0xdd为8字节hash。当前C++health仅err>=0接受短读；USB兼容层必须加精确长度/版本门槛，不能补零健康。 参考DP又是health17/CAN4/CANhealth5，不能因同为DOS就按SP098的health16解包。 来源：L03, P02, P03, P04, O02, R06。
- **F04** 主机src以4为板间stride，但MCU只应接本地0/1/2。返回/拒绝用+0x80/+0xC0表示；不是可原样写回sendcan的端口号。 来源：O01, O03, D05, P01, P06, R02。
- **F05** 现有selfdrived只遍历收到的pandaStates，不能单靠该循环发现末尾板消失；hardwared/UI还有[0]和任意点火OR假设；CarParams/passive、CanBusBase和诊断查询也带单板/最后块假设。 来源：O12, O13, O14, O15, D01, D02, D03, D04。
- **F06** 参考DOS不支持CAN-FD。当前host header首bit名reserved而MCU为fd；正式C++通过每bus CAN-FD auto工作。FD能力与传输USB/SPI、payload长度不可等同，<=8byte也可能是FD帧。 来源：O01, O03, O06, D05, P05, R04。
- **F07** 官方与旧包装器均有reset/flash/recover路径，构造Panda也会做写操作；设计中的发现/只读能力验证与正式初始化必须拆开。 来源：O02, O05, O10, L01。

## 稳定slot与总线语义

板型、物理身份、逻辑slot、车辆角色四者分开。slot只来自未来显式绑定，不由枚举顺序决定，也不规定DOS第一。每板的身份、transport、协议/构件manifest、角色和配置hash在会话内冻结。真实序列号只放本地私有配置。

| 对象 | 含义 | 本设计约束 |
| --- | --- | --- |
| physical board identity | 某一块板，不是DOS/red类型名称 | 未绑定、重复、歧义即隔离 |
| logical slot | 固定0或1 | 掉线后留空位，不把另一块移过来 |
| local MCU bus | 板上0/1/2 | MCU safety只使用本地编号 |
| host bus | 4×slot+local bus | slot0为0/1/2；slot1为4/5/6；3/7保留 |
| returned/rejected src | 正常host bus加0x80/0xC0之一 | RX回执状态，绝不可送回TX；两标志同时出现拒收 |
| roles | vehicleControl/diagnostic/harness/peripheral/ignitionSources | 独立选择，可同板，不隐含等于slot0 |

两组必测SRC2夹具都使用虚构身份：A=DOS、B=外置H7。SRC2-A绑定A→1、B→0；SRC2-B绑定A→0、B→1。各自反转枚举顺序并把外设、诊断、车辆角色分配在不同slot上；结果必须只取决于绑定。它们不是这台机器的接线结论。

当前CanData.src是UInt8。例如slot1/local0的正常、returned、rejected分别是4、132、196；MCU端口始终是0。不能把returned/rejected当独立高位掩码随意清除：两flag相加是320，再缩成UInt8可能伪装普通src，所以一开始即拒绝非法组合。

## 按模块修改的设计

### H01 固定拓扑与身份，不按发现顺序排板

拟修改：`proposed: openpilot/selfdrive/pandad/topology.{h,cc}`; `proposed: downstream profile loader`; `openpilot/selfdrive/pandad/main.cc`。

采用版本化双required-slot模板，slot只能为0、1；每slot未来由可信配置绑定唯一设备身份、预期板型/MCU、明确USB或SPI、协议profile。roles包含vehicleControl、diagnostic、ignitionSources、harness、peripheral；不由DOS/internal/type排序推角色。真实serial仅留本地配置，不进版本库。USB物理路径只作核对线索，换hub不能自动换板。未知、重复身份、一个设备同时暴露USB/SPI、同slot多候选均隔离，严禁自动选择、重排、缩短。profile hash在一次运行中不可变，重启仍按同一绑定。模板本轮不被任何runtime读取。

依据：O01, O08, L01, R01, R02, R03；预期验收：T01, T02, T03, T04, T05。

### H02 分离传输、协议与板能力

拟修改：`openpilot/selfdrive/pandad/panda_comms.h`; `openpilot/selfdrive/pandad/panda.{h,cc}`; `openpilot/selfdrive/pandad/spi.cc`; `proposed: openpilot/selfdrive/pandad/usb.cc`; `proposed: packet_codec.{h,cc}`。

恢复窄PandaTransport接口，USB和当前SPI各自实现。factory只按绑定transport创建，不用USB失败后任意回退。codec纯内存、每板独立接收残片；协议握手先于任何配置写。USB精确处理control返回长度、partial bulk、超时/STALL/no-device和关闭竞态，handle锁按设备。失败写入若已发送字节不明，不重试整包；先使该generation失效。保留当前SPI协议，不能复制旧2048字节头覆盖新4096/id/CRC链。接口选择按VID/PID、预期interface/endpoint和身份白名单；firmware/bootstub分开，unknown隔离。 短头必须先判remaining>=6，禁止照抄size为uint32时的pos<=size-sizeof(header)减法边界（0..5字节会下溢）；所有payload/缓冲区长度先验证再读。二进制fixture核little-endian与6byte布局，不把平台C++位域布局默认当跨平台合同。

依据：O02, O03, O04, O05, L02, R02, P04；预期验收：T06, T07, T08, T09, T10。

### H03 统一目标ABI，旧ABI只离线解码

拟修改：`proposed: protocol_manifest and capability table`; `openpilot/selfdrive/pandad/panda.cc`; `openpilot/selfdrive/pandad/pandad.py`。

正式目标为经独立实现/验收的DOS/F4和外置H7使用当前统一CAN/health/safety ABI；旧16/4/5仅显式legacy离线fixture，不把旧health翻译后直接放行目标控制。协议回复长度、hash、health大小、CANhealth大小、板型及期望构件签名必须同profile一致。未知/旧版/短读为quarantined或invalid，绝不启用BOARDD_SKIP_FW_CHECK，不自动刷写修复。按构件manifest匹配specific board+MCU，不能任意panda.bin或panda_h7签名命中就接受。 DP health17另立参考解码fixture，与SP098 health16互相拒收，均不能取得目标控制资格。

依据：O02, O10, L03, P02, P03, P04, R06；预期验收：T11, T12, T13。

### H04 bus和flag单点编码

拟修改：`proposed: bus_router.{h,cc}`; `openpilot/selfdrive/pandad/panda.{h,cc}`; `openpilot/selfdrive/pandad/pandad_api_impl.py`。

正规host bus=4*slot+local_bus，其中local_bus只能0/1/2；3和7保留，非端口。TX先拒绝returned/rejected标记及未授权bus，再按slot路由并减offset，只发送本地编号。RX先验证local_bus和互斥flag，再加slot偏移和一个状态偏移；正常0/1/2/4/5/6，returned128/129/130/132/133/134，rejected192/193/194/196/197/198。两flag同时置位拒收，避免+320溢出UInt8；不能用src&0x7等掩码把回执伪装普通流。每板MCU safety仍按本地总线定义，host4/5/6永不进入MCU allowlist。

依据：O01, O03, O16, D05, P01, P06, R02；预期验收：T14, T15, T16, T17。

### H05 能力门控与FD语义

拟修改：`proposed: capability table`; `openpilot/selfdrive/pandad/pandad.cc`; `openpilot/selfdrive/pandad/panda.cc`。

DOS/F4只允许经典CAN合法长度0..8，不发FD auto/data-rate/non-ISO命令；H7 FD仅在已验证每bus配置下允许。USB是否高速不决定CAN-FD。纯codec保留raw fd bit用于校验/诊断；不把fd塞进src。当前cereal CanData不承载FD位，v1保持H7官方auto语义并把总线模式作为profile限制；若要求同一总线逐帧区分经典/FD的<=8byte TX，v1不能表达，作为后续显式API/schema议题，禁止猜测。F4收到fd标记或>8byte视协议/能力错误。

依据：O01, O03, O06, D05, P05, R04；预期验收：T18, T19。

### H06 双板生命周期与失效不重映射

拟修改：`openpilot/selfdrive/pandad/pandad.{h,cc}`; `proposed: board_session.{h,cc}`; `proposed: tx_gate and scheduler`。

状态为unbound→discovered→identity/protocol verified→configured-noOutput→two-board safety-readback barrier→ready；任一缺失/失联/短读/身份漂移/签名不符转group-invalid。generation和单调时钟标识会话；每板RX残片与TX队列只属于其generation。任一required板断连立即关闭所有真实TX资格（本轮仅mock gate），取消队列；不将幸存板挪slot，不单板降级。重连只接受原身份原slot，清空旧缓冲，重新核协议与两板读回，且仅在offroad开始新控制epoch；不自动恢复原先engaged状态。新增未知板不参与运行。每板有有限deadline/工作量，慢速DOS不能阻塞另板心跳，100Hz CAN聚合/10Hz健康等上游节奏用fake clock先证明，再留实时实机验收。 TX还必须检查sendcan Event.valid，不能只验证时间戳；RX保留原始采集时间与generation，聚合发布不能给旧帧刷新年龄。MCU拒绝/阻止转发后不能改走另一板重发。时限、队列容量、退出与失联转移预算在阶段0冻结后测边界，不能使用无限等待作为默认。正常退出、已接合退出、持锁断连与断电分别验证，不假定一种继电器动作适用于全部情形。

依据：O06, O07, R03；预期验收：T20, T21, T22, T23。

### H07 保留cereal布局，固定两条状态且整组有效

拟修改：`openpilot/selfdrive/pandad/pandad.cc`; `openpilot/selfdrive/selfdrived/selfdrived.py`; `openpilot/system/hardware/hardwared.py`; `openpilot/selfdrive/ui/ui_state.py`。

v1明确不改cereal，pandaStates始终长度2且index=slot，两个都是required。任何slot缺失、health过期、短读、版本不符或身份异常，整个Event.valid=false，CAN聚合event亦invalid并禁止控制；不缩列表。缺位只保留last-known板型身份（尚无则unknown），其他占位字段不代表新健康回复，controlsAllowed始终false。有效健康从不补零，不把unknown/默认noOutput当健康通过。具体slot存在/age/error/generation在主机结构化诊断中记录。消费者必须同时校验Event valid、alive/fresh、长度=2和冻结profile；任何失败整组不可用，不依赖placeholder值判安全。当前schema无逐板valid，故不支持局部健康降级；若未来要支持，必须单独审查新增schema。

依据：O06, O12, O13, O14, O16；预期验收：T24, T25, T26。

### H08 safetyConfigs与板级读回屏障

拟修改：`openpilot/selfdrive/pandad/panda_safety.cc`; `openpilot/selfdrive/pandad/pandad.h`; `openpilot/selfdrive/car/card.py`; `openpilot/selfdrive/selfdrived/selfdrived.py`; `proposed: Song PLUS interface topology adapter`。

safetyConfigs固定按slot长2，非控制板明确noOutput；passive也生成两个noOutput，不能缩成1。分别设置alternativeExperience/model/param并读回，两板均匹配且无安全故障才允许进入ready；ControlsReady/FirmwareQueryDone本身不是全板屏障。空/短/长配置、板型/能力不兼容、旧epoch CarParams一律fail-closed。selfdrived补预期数量与有效性检查，仍保留现有safety mismatch/relay/controlsAllowed全板检查；不以任意一板controlsAllowed=true代替全部控制板条件。将来真实TX还须单独完成设备阶段授权、匹配固件/车型safety与实机门槛；本轮SEND gate只模拟。 health中未知fault/enum、非有限数和单位/范围错误也不能补成正常值。

依据：O09, O12, O15, D02；预期验收：T27, T28, T29。

### H09 角色化点火、外设、线束与诊断

拟修改：`openpilot/selfdrive/pandad/pandad.cc`; `openpilot/system/hardware/hardwared.py`; `openpilot/selfdrive/ui/ui_state.py`; `openpilot/selfdrive/pandad/panda_safety.cc`; `opendbc/car/car_helpers.py`; `opendbc/car/fw_versions.py`; `proposed: immutable role policy`。

peripheral/harness/diagnostic/vehicleControl及点火源各自从冻结profile选slot，可同板但不隐含相同。保留每板raw ignition与fault，不改写DOS原始ignition来假装已验证；聚合点火只使用明确允许的slot和line/can来源，并要求整组新鲜有效。所有消费者使用同一策略，禁止hardwared/UI继续任意OR或固定[0]。DP的DOS+red假点火特例仅作待验证线束问题，不复制其基于数组顺序的屏蔽。fan/IR只送已验证peripheral能力所属板，故障不能导致重发到另一块；IR/driver监控缺硬件仍是独立缺口。OBD/ELM327只给diagnostic role，车辆ECU主动查询是TX不属只读；VIN0/1及Request.bus需通过显式诊断映射一致处理。 全局relay/safety/heartbeat故障按所有required板聚合，不能只看vehicleControl板；peripheralState的有效性也受角色板及整组失效约束。 deviceState/风扇请求过期不得当成新指令；整组控制失效不等于关闭散热。内部板失联时的风扇、心跳与降级告警行为需在能力manifest及故障预算中明确，不能把外设控制重定向到另一块板。

依据：O07, O09, O13, O14, D03, D04, R03, R04, R05；预期验收：T30, T31, T32。

### H10 车型bus和回放来源显式化

拟修改：`opendbc/car/__init__.py:CanBusBase`; `proposed: Song PLUS topology/CanBus`; `openpilot/selfdrive/car/card.py`; `openpilot/selfdrive/test/process_replay/migration.py`。

宋适配不得仅由len(safetyConfigs)或fingerprint最大bus推车辆所在slot；用角色映射生成显式CanBus offset并在CP两槽配置中保持一致。v1不全局改变其他官方车型推断，可为宋增加显式offset接口/适配器并保留上游默认路径。历史日志必须有producer版本和排序provenance，SP/DP各自映射；无板绑定元信息的日志不猜bus所属硬件。回放迁移不得用当前[-1]统一覆写安全参数掩盖双板错序。CarParams cache绑定拓扑profile hash和协议generation，失配不复用。

依据：D01, D03, O15, O17, L01, R01；预期验收：T33, T34, T35。

### H11 默认不reset/flash的入口与独立离线编译边界

拟修改：`openpilot/selfdrive/pandad/pandad.py`; `openpilot/system/manager/process_config.py`; `openpilot/selfdrive/pandad/SConscript`; `proposed: offline mock targets`。

将来兼容profile使用独立wrapper入口：默认只拒绝不匹配，不自动reset/recover/flash；不直接调用当前官方或SP/DP wrapper。固件维护拆成明确单板manifest的独立流程，不在manager重启/热插拔链里。未知板quarantine，不使用BOARDD_SKIP_FW_CHECK或allOutput作为兼容方案。抽出codec/router/state-machine为无设备I/O测试目标；Linux USB/SPI构建与macOS纯逻辑测试分开声明，macOS旧minimal成功不能证明AGNOS pandad。测试入口不import Panda构造，不启动manager/pandad，不触碰USB/串口、GPIO、CAN或联网。本轮不实现或构建这些目标。

依据：O10, O11, O19, L01, R01；预期验收：T36, T37。

## 推进顺序与完成标准

| 顺序 | 工作 | 产出与退出条件 |
| --- | --- | --- |
| 0 | 冻结host contract与两组SRC2 fixture | 逻辑slot/role/template、host↔local表、失效语义、历史排序标签；本阶段没有真实身份绑定。 设计互相一致；两角色fixture覆盖，不要求照片才能开始。 |
| 1 | 实现纯codec、bus router、协议manifest与能力表（后续授权） | 无I/O库和golden fixtures；legacy与target显式分开。 T06–T19预期全通过；无设备运行或固件构建。 |
| 2 | 实现mock transport与稳定slot生命周期 | 显式USB/SPI factory接口、fake-clock group状态机、generation/队列屏障。 T01–T10、T20–T23预期通过；不会重排/重放。 |
| 3 | 接入双板状态、安全配置与所有消费者 | 固定2条pandaStates、整组valid、角色策略、CarParams两槽/显式bus、回放适配。 T24–T35预期通过；任意缺板/错序/单板健康不能取得ready。 |
| 4 | 加入真实USB实现及无自动维护wrapper，仅离线编译 | USB依赖锁/构建目标；Linux与纯逻辑主机结果分别记录；manager入口审查。 T36–T37及Linux编译预期通过；不会触碰设备；本轮尚未实施/编译。 |
| 5 | 进入独立设备阶段前的门槛 | 本机/外置板真实身份、MCU/板修订、线束与点火/外设角色、匹配固件和安全实现、恢复条件、阶段授权。 门槛不足只阻止真实I/O/控制，不阻止前面离线设计和实现；驾驶员相机/告警等完整系统缺口另行解决。 |

硬件缺资料不会挡住前四项离线工作。进入设备阶段前仍必须核身份、真实板能力、角色/线束、匹配固件和恢复条件；驾驶员监控和告警的完整系统缺口不由本兼容层消除。真实TX只能在这些门槛、两板safety读回屏障及设备阶段授权全部成立后另行考虑。

## 离线验收用例（全部expected，未执行）

| ID | 输入/故障 | 预期结果 |
| --- | --- | --- |
| T01 | SRC2-A: synthetic DOS=A, external H7=B; bind A→slot1, B→slot0; enumerate [A,B] and [B,A]. | Both permutations produce identical slot map and host buses; no claim that these are actual unit bindings. |
| T02 | SRC2-B: same synthetic identities; bind A→slot0, B→slot1; reverse enumeration. | Both permutations still preserve explicit binding; peripheral/diagnostic/vehicle roles do not follow first/internal. Tests also use same hardware types with two distinct identities. |
| T03 | Missing slot0 or slot1 at startup; unexpected third identity or duplicate same identity. | Required map remains two entries; group invalid, no TX, unknown identity quarantined and not opened/configured automatically. |
| T04 | Same identity visible on USB and SPI, or same slot has two matches; USB port/hub path changed. | Ambiguity rejected; no fallback or remapping. Only configured transport+identity accepted. |
| T05 | Restart or attempt profile update while onroad. | Same frozen profile yields same slots across restart; changed profile cannot activate within active epoch. |
| T06 | Every possible split boundary of 6-byte header+payload; zero-byte and concatenated USB packets. | Exact frames reconstructed once, per-board; no bytes from one slot enter another slot parser. 对0..5byte partial header，以ASan/UBSan离线目标验证无unsigned下溢或越界；该目标尚未构建/执行。 |
| T07 | Control health returns 0, short, too long, error, or wrong version. | No zero-padding, no healthy state, no safety barrier completion; group invalid. |
| T08 | Bulk write partly completed then timeout with known/unknown transferred count. | No replay of whole packet; uncertainty invalidates generation and clears pending TX. No duplicate control frames inferred safe. |
| T09 | Disconnect between enumerate/open/read/write/close; concurrent heartbeat and CAN. | No use-after-free or crossed handle; bounded cleanup, original slot stays reserved, no output to another board. |
| T10 | USB mock selected; SPI mock selected; old SPI header presented to new SPI parser. | Selection is explicit; old frame rejected; USB selection never opens SPI or resets GPIO. |
| T11 | SP098 legacy 3-byte [16,4,5], DP legacy [17,4,5], and target 8-byte two-hash replies. | Legacy fixture decodes only under legacy offline label, cannot qualify for target ready; no protocol guessing by prefix. SP and DP health decoders reject each other despite sharing CAN4/CANhealth5. |
| T12 | Wrong firmware signature, right signature for wrong MCU/board, unknown capability. | Manifest mismatch quarantines; no flash, reset, bypass flag or TX. |
| T13 | Target health flags and boundary-value fields; truncated alternativeExperience/safetyParam. Include unknown fault/enum, unit boundary and non-finite decoded values. | Exact golden values preserved with correct signedness/width; missing values are invalid, not 0. Unknown/invalid health never becomes a zero-filled healthy report. |
| T14 | Each slot×local bus0/1/2, both SRC2 role fixtures. | host4/5/6 route only to slot1 header.bus0/1/2; no MCU bus4/5/6, no other slot TX. |
| T15 | TX sources3,7,8,128..255 and returned/rejected examples132/196. | Reject reserved/unmapped/status sources before transport; never strip flags into ordinary TX. |
| T16 | RX normal, returned only, rejected only, both bits; local bus3..7. | Valid flags produce exact src tables; both bits/reserved bus rejected without UInt8 overflow or wrap. |
| T17 | Bad checksum or incomplete frame at end of old generation then reconnect. | No publish of corrupted frame; group invalid; clear old residual. Offline codec records recovery intent without control_write. |
| T18 | F4 payload0..8 incl8, length9/12/64, fd bit on <=8; FD config requests. | Classic0..8 accepted by capability mock; all FD/oversize/config writes rejected for F4. H7 accepts only supported legal DLC and configured mode. |
| T19 | H7 same <=8 bytes as Classic and FD, FD auto transition, impossible DLC lengths9/10/11. | Raw fd preserved in diagnostics; no claimed differentiation in current CanData; ambiguous per-frame TX unsupported, illegal lengths rejected. |
| T20 | Required board disconnects during engaged mock; pending messages exist on both slots. | Group TX gate closes immediately in simulated event step, both queues clear; slots unchanged; heartbeat engaged false and controller fault visible; no automatic resume. A firmware rejected or forwarding-blocked frame must not be retried through the other board. |
| T21 | Reconnect same identity with new uptime/generation or different identity at same port. | Same identity must revalidate entire barrier; different identity quarantines; stale CAN/health/CP/sendcan from old epoch rejected. |
| T22 | Fake-clock slow DOS bulk timeout while H7 heartbeat is due. | Per-board bounded work prevents head-of-line blocking; heartbeat deadlines tracked, missed deadline invalidates group; no claims about real C3XL latency. Freeze numeric deadline/queue budgets before implementation acceptance and test just-before/at/after boundaries. |
| T23 | Late sendcan (>1s upstream bound), future timestamp, previous-epoch sendcan. Include Event.valid=false with an otherwise fresh timestamp. | Drop; reconnect cannot replay buffered actuation. Ready barrier and current epoch also required for otherwise fresh frames. Invalid sendcan is never eligible; aggregator publishing must not renew old RX frame age. |
| T24 | Last slot disappears after two valid states. | pandaStates length stays2, event.valid=false; placeholder controlsAllowed=false; selfdrived does not treat shortened list or silent default as success. |
| T25 | Fresh envelope but one stale health or one missing/unknown state. | Group validity false; hardwared/UI/selfdrived ignore all health for readiness and expose communication failure; no stale ignition start. |
| T26 | Event.valid=false while raw ignition/old controls flags would otherwise be true. | All consumers reject readiness; no first-board exception, no healthy interpretation of placeholder. |
| T27 | SafetyConfigs length0/1/3, wrong order/model/param/alternativeExperience. | Reject before ready; exactly two slot configs required, unchanged model/enum meaning. |
| T28 | Only one board reports expected safety; second stale/no reply or fails after initial readiness. | No target TX before both valid readbacks; later failure closes group gate. One controlsAllowed true never substitutes other required control board. |
| T29 | Passive mode; one vehicle-control role and one peripheral-only role. | Two noOutput entries in passive; active mock explicit noOutput for non-controller and validated model for controller. Device stage authorization remains false for all tests. |
| T30 | SRC2-A and B with peripheral/vehicle/diagnostic on opposite slots; mock fan/IR/harness. Include stale deviceState/fan requests and internal peripheral-board loss. | Each request targets role owner only; no duplicate writes or use of array[0]; unverified peripheral capability causes explicit unavailable state. Do not equate invalid control group with fan-off or silently move thermal control to the other board. |
| T31 | Conflicting ignitionLine/ignitionCan, non-authority DOS high, authority low, absent authority. | Raw states retained; aggregation follows same configured sources in pandad/hardwared/UI; absent/stale authority cannot start; no copy of DP order-based quirk. |
| T32 | OBD multiplex/VIN/FW query under each role fixture and passive/offline gate. | Only diagnostic slot and translated local bus receive mock request; no real query; unsupported role/bus rejects and cannot leak to other board. |
| T33 | Two safetyConfigs but vehicle role slot0; only slot1 fingerprint populated; inverse fixture. | Explicit Song bus mapping stays role-selected, never blindly4*(len-1) or fingerprint max; other official default behavior unaffected. |
| T34 | Historical SP098 and DP traces with known producer ordering; unlabelled trace. | Separate mapping provenance, no automatic DOS-first assumption; unknown trace cannot become board-confirmed evidence. |
| T35 | CarParams cache from other topology/profile; legacy migration writes[-1]. | Reject cache for target session; no migration that overwrites wrong slot to force mismatch pass; original fixture retained. |
| T36 | Boot wrapper mock mismatch, process restart, unknown hotplug, DFU device. | No reset/flash/recover calls; no bypass env; events diagnostic only. New profile is inert until explicit future implementation. |
| T37 | Offline target with file/device/network access denied and all transport calls replaced by mocks. | No device access or writes; fake time and fixed seed reproduce all scenarios. Linux USB/SPI compile, native pure-code tests and device tests reported separately when eventually run. |

验收执行记录应另存commit、profile hash、fixture版本、随机seed、fake-clock轨迹、pass/fail/skip及被禁止的I/O计数。本报告没有测试通过数，37项只是设计出的用例；现有官方codec测试并未覆盖这些双板生命周期条件。

## 可复核源码锚点

每项的仓库根、commit、文件、行范围及SHA-256同时保存在JSON。SP098归档没有.git元数据，commit明确为unknown，不能用DP提交冒充。源码只是静态读取，没有导入执行。

- **O01** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/panda.h`，行23–37, 46–60。C++ handle固定SPI；stride=4、returned/rejected偏移和CAN header。 SHA-256 `325fbcc720a9af7988ec8098a32568f85ed923f55508e829c6a5fe3d3ca210af`。
- **O02** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/panda.cc`，行15–37, 87–96, 116–126。构造SPI并reset communications；健康短读只判断err>=0；签名匹配旧文件名不等于F4构建支持。 SHA-256 `b5a198a30ff5a193e5c71e2c0bc2b2649a5d6737ae13bee4ec7bbba471eec9b9`。
- **O03** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/panda.cc`，行168–207, 242–280。单板TX只接受src<4；RX按两个flag分别相加；分片缓存和checksum错误触发reset。 SHA-256 `b5a198a30ff5a193e5c71e2c0bc2b2649a5d6737ae13bee4ec7bbba471eec9b9`。
- **O04** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/panda_comms.h`，行14–35。只有PandaSpiHandle，无可供USB继承的当前抽象类。 SHA-256 `4650dd369920307196aa3904d4d201630e2cf5d07a3a199f426387de2b1cef5d`。
- **O05** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/spi.cc`，行65–106, 299–309。SPI实例化开设备并配置IOCTL；带id的当前SPI协议与旧头不等同。 SHA-256 `61c0c34d4e3d843bce21fb095623994a6a16d60bacf25fad5cf29ab151d60aab`。
- **O06** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/pandad.cc`，行26–56, 59–83, 166–218, 239–258, 262–278。单板连接、CAN-FD自动配置、过期TX过滤、长度1状态、点火/电源/外设/心跳。 SHA-256 `db81105a5cdb619ad1f90cde776735b7b2225a6833de71e8a421111177040221`。
- **O07** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/pandad.cc`，行281–307, 333–357, 362–423, 426–451。外设和调度均用同一Panda；100Hz循环、10Hz健康、20Hz外设；无参数取首个serial。 SHA-256 `db81105a5cdb619ad1f90cde776735b7b2225a6833de71e8a421111177040221`。
- **O08** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/main.cc`，行8–20。命令行仅使用argv[1]的单serial入口。 SHA-256 `a22d355939882580508344948971ba4b7354bd22acbbd5f6ed0808dc88b54f2d`。
- **O09** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/panda_safety.cc`，行5–38, 40–69。单板ELM327和OBD、ControlsReady/FirmwareQueryDone，且只读safetyConfigs[0]。 SHA-256 `5cca50b8a47e498a73e47f583d1cbff8e820810a9fc07bfb2e61de0f8850354d`。
- **O10** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/pandad.py`，行14–45, 62–86。官方wrapper枚举、重置内部板；不ready就恢复/刷写。不能原样用于本兼容层。 SHA-256 `b4a845934d813aaf40ac9b59c7bd1d954635e70b342ab9dc1923129abba641be`。
- **O11** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/SConscript`，行1–10。非Darwin才构建pandad；当前只编panda.cc和spi.cc，无USB库。 SHA-256 `312960ac0bfd2c1e39d0d0e45fba9fb47b24425f2636e2616c4bbf6d50d7c1f0`。
- **O12** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/selfdrived/selfdrived.py`，行325–339, 498–507。按收到列表index对safetyConfigs；缺尾部板不会被该循环独立发现；检查各非忽略模式controlsAllowed。 SHA-256 `632ea5bc4c12247b55fbe7060e390fe7064fb8f3cfaa85df5bea37297e1c3c40`。
- **O13** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/system/hardware/hardwared.py`，行259–280。点火OR所有非unknown板，in_car取列表[0]harness；现有分支未在此处验证Event.valid。 SHA-256 `5d94bd66a393e0abe150a626168fcc2ba697ed70c57675dd7b9d84735a1f590f`。
- **O14** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/ui/ui_state.py`，行158–170。UI用[0]板型，并OR所有点火。 SHA-256 `ac7565e8f3de4bbe4f63cf797474dda954aa1f1a7776f7779ab367bf207a1f95`。
- **O15** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/car/card.py`，行85–118, 141–150, 160–167。车型发现未传拓扑；passive将safetyConfigs缩成1；CP被持久化且CAN进入车型接口。 SHA-256 `e09855ec8d8f6d5b34acd0a88686b90380bcbb1794726a90d237eb77f4f3dc39`。
- **O16** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/cereal/log.capnp`，行407–410, 531–575, 614–625。CAN src为UInt8；PandaState无serial/slot/present字段，但枚举仍保留DOS。枚举不是运行支持。 SHA-256 `60ea81bac323120e04a456901e4bbd349a377a3db53a568d6870e9371859cc10`。
- **O17** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/test/process_replay/migration.py`，行319–345。旧route migration可能取CP[0]而写pandaStates[-1]；不可直接给双板验收纠正样本。 SHA-256 `03db5edf12942871b4c9d354d3a4d192939f8134b237e29fc82636f709435fea`。
- **O18** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/selfdrive/pandad/tests/test_pandad_canprotocol.cc`，行20–39, 69–110。现有codec测试无稳定slot、断连或身份场景；不能据DOS枚举测试推断runtime支持。 SHA-256 `9349f5717b5d526655b1be36e01d491a267f5a2e5aec86595fce37c817433170`。
- **O19** `upstream-openpilot` @ `35e78a9d9518855ec0dacec919e719624a28131f`；`openpilot/system/manager/process_config.py`，行97–107。manager默认启用Python pandad wrapper。 SHA-256 `22f4e34c33034da326a2c6ca0563ea31aebfd703d3f158d290fc00b4ed177f56`。
- **D01** `upstream-openpilot/opendbc_repo` @ `b3bd79a659dae961af169a3baac723961f720133`；`opendbc/car/__init__.py`，行114–123。CanBusBase由fingerprint最大bus或len(safetyConfigs)决定最后一个4bus块。 SHA-256 `0e4a16467d400ea89852ad3e18206231948bcba61ea67ad48637b9ed18f760bc`。
- **D02** `upstream-openpilot/opendbc_repo` @ `b3bd79a659dae961af169a3baac723961f720133`；`opendbc/car/car.capnp`，行478–479, 528–535。SafetyConfig仅model/param；list位置即映射语义，需要拓扑约束但不能改enum凑兼容。 SHA-256 `f39cc7be3216e3b692334b4c7dc7fc5f6509865e700b15e17acccc7b7cc644ab`。
- **D03** `upstream-openpilot/opendbc_repo` @ `b3bd79a659dae961af169a3baac723961f720133`；`opendbc/car/car_helpers.py`，行42–64, 100–107。fingerprint保存所有src<128，但legacy匹配bus0/1；VIN也在0/1查询。 SHA-256 `f64b651d216fe7799ca58144141ab73368b3bb34c3476cf71e793ead403562b3`。
- **D04** `upstream-openpilot/opendbc_repo` @ `b3bd79a659dae961af169a3baac723961f720133`；`opendbc/car/fw_versions.py`，行175–203。FW查询使用Request.bus，OBD回调没有板slot参数。 SHA-256 `701acd286791b75f7151a73256a1b28f268cbf3ee6ece5d157cb1dc87c7dc9ca`。
- **D05** `upstream-openpilot/opendbc_repo` @ `b3bd79a659dae961af169a3baac723961f720133`；`opendbc/safety/can.h`，行3–18。当前MCU CAN包有fd位、3bit本地bus、DLC、returned/rejected和64byte上限。 SHA-256 `76f2ab751f89797c0a2254ab9a010a261678fe30f869d4e777cb7f2048802743`。
- **P01** `upstream-openpilot/panda` @ `a59195163c32a3a375ea6c86715dc08fd0984c1b`；`board/can.h`，行1–5。MCU CAN端口数3；host stride4不表示第4个物理端口。 SHA-256 `ee30d6a723476e5bfaf5493249af715c0f783b95fe4cdc19cb868da09b2334e8`。
- **P02** `upstream-openpilot/panda` @ `a59195163c32a3a375ea6c86715dc08fd0984c1b`；`board/health.h`，行3–35, 37–64。当前health flags和字段布局；健康必须按精确协议/长度解释。 SHA-256 `f512bcff6925e233a35c00e2d921fe01933fd77c0705c573a233b170d73ce20d`。
- **P03** `upstream-openpilot/panda` @ `a59195163c32a3a375ea6c86715dc08fd0984c1b`；`board/main_comms.h`，行222–235。0xdd为两个uint32版本hash，共8字节；0xde限制本地bus<PANDA_CAN_CNT。 SHA-256 `57c717ac0434b16e6026e46d006d72a89be8661d9e51c3e1a99719b524c0a203`。
- **P04** `upstream-openpilot/panda` @ `a59195163c32a3a375ea6c86715dc08fd0984c1b`；`python/__init__.py`，行55–103, 125–140, 617–621。当前Python USB PID、版本hash和编解码；不能据其USB支持认定C++runtime支持。 SHA-256 `20815dd095ba76bdb9d095e33ddbba62a7ccabf7bd4e87bdc61dee147e9fb04a`。
- **P05** `upstream-openpilot/panda` @ `a59195163c32a3a375ea6c86715dc08fd0984c1b`；`board/drivers/fdcan.h`，行110–117, 127–135, 180–188。FD自动模式与returned/rejected合法互斥取值。 SHA-256 `c2dc76a42e039414b1f8b384cec8e3eae47f8f4eeea68d92150b843598f6f150`。
- **P06** `upstream-openpilot/panda` @ `a59195163c32a3a375ea6c86715dc08fd0984c1b`；`board/drivers/can_common.h`，行161–175。safety拒绝设置returned=0,rejected=1；本地bus<3才进入CAN队列。 SHA-256 `1509297d8ce4f8a527c3bf941dae3f82a26db82a4c3397fd324bd32b81ec9ec1`。
- **L01** `../op_rootdir/data/openpilot` @ `unknown (local archive; SHA-256 identified)`；`selfdrive/pandad/pandad.py`，行119–124, 160–163, 24–41, 91–109。SP098归档内部板最后排序，并有自动刷写；未取得该C++实现源，不能冒充DP。 SHA-256 `9a095c672dffb6cc492e307876d7a670f481ab3d0d22ee1c1a01190df986307a`。
- **L02** `../op_rootdir/data/openpilot` @ `unknown (local archive; SHA-256 identified)`；`selfdrive/pandad/panda_comms.h`，行20–55, 58–87。SP098归档存在USB/SPI抽象接口；USB实现源未在此归档提供，旧SPI头与当前不同。 SHA-256 `9dd1d89a161b3e053192bcf9eb7fba0a1fedee6627158ff6d065a902d118684d`。
- **L03** `../op_rootdir/data/openpilot` @ `unknown (local archive; SHA-256 identified)`；`panda/python/__init__.py`，行158–177, 722–729。匹配已采集实机hash的旧API常量16/4/5和3字节版本回复；DOS属于F4分类。 SHA-256 `c2df6b142adb4ae1a4163128760a8e03a4659eae349b3f7b062231cec7a762c7`。
- **R01** `research/evidence/public-source/dragonpilot-94ad85cf` @ `94ad85cfda64773a960d048dc11cac02d4aa86a5`；`selfdrive/pandad_tici/pandad.py`，行125–130。DP内部板第一，排序与SP098相反。 SHA-256 `36e9bc2ea2b13773d4c0ce9436fc0724a15e0863868bff95e652366dcafb8f68`。
- **R02** `research/evidence/public-source/dragonpilot-94ad85cf` @ `94ad85cfda64773a960d048dc11cac02d4aa86a5`；`selfdrive/pandad_tici/panda.cc`，行15–58, 196–211, 285–293。DP USB优先SPI回退、去重枚举与每板加减bus_offset；仅静态参考。 SHA-256 `9f3628422e9b5a49e56a60caffa5e4a568abc2d93639320143a6a9eeca031299`。
- **R03** `research/evidence/public-source/dragonpilot-94ad85cf` @ `94ad85cfda64773a960d048dc11cac02d4aa86a5`；`selfdrive/pandad_tici/pandad.cc`，行21–38, 56–59, 205–238, 326–364, 432–445, 500–527。DP枚举索引即slot、全板重连、[0]外设、DOS+red点火特例依赖顺序。 SHA-256 `0d32bee3faf988ac9a7af8c64e4fb76a4230ddc494c0ab057a70b292375f4926`。
- **R04** `research/evidence/public-source/dragonpilot-94ad85cf` @ `94ad85cfda64773a960d048dc11cac02d4aa86a5`；`panda_tici/board/boards/dos.h`，行130–157。参考DOS board has_canfd=false；风扇/IR等功能存在，但本机PCB仍不能据此认定。 SHA-256 `ec15964f5d3cb106a0fb5932fb34d71e1d2bd5d01780c979eaeec471318a42ed`。
- **R05** `research/evidence/public-source/dragonpilot-94ad85cf` @ `94ad85cfda64773a960d048dc11cac02d4aa86a5`；`panda_tici/board/stm32f4/board.h`，行18–29。第三方Lite GPIO识别微调只是该分支事实，不能仅凭外观/名称移植。 SHA-256 `f65f4c1d45a1a56a8b0c70f166424778d13a68708981d48b0954f1e85c899c5b`。
- **R06** `research/evidence/public-source/dragonpilot-94ad85cf` @ `94ad85cfda64773a960d048dc11cac02d4aa86a5`；`panda_tici/python/__init__.py`，行130–134。DP参考health版本17，不同于SP098的16；CAN4/CANhealth5相同不能证明health兼容。 SHA-256 `474bb40626070bd1b85c7b81fad2aa1c4de3eb06f0e9562948795c5cbfe71483`。
