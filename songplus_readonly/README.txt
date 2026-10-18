宋 PLUS 独立只读 CarState 原型
车辆：2023 宋 PLUS DM-i 冠军版 110 km 旗舰 PLUS

用途与范围
这是一套本地主机离线接收解析原型，使用官方目标栈 opendbc 的 CANParser、
DBC 信号解析、checksum/counter 实现以及 CarState schema。目标提交固定为
b3bd79a659dae961af169a3baac723961f720133，路径为 upstream-openpilot/opendbc_repo；
启动时核对导入路径、提交和受跟踪源码。历史 upstream-opendbc 不能替代此环境。

独立 DBC 仅含八个接收报文，没有发送报文、CarController、车型注册、车辆匹配、
Panda 实例或 safety 改动。不读取设备参数，不导入 Mr One 车型实现。
旧系统记录的 carState 只用于离线对照，不是独立实车真值。

最新实现已接入gasPressed、brakePressed和cruiseState.standstill三个候选，
来源见 ../offline-analysis/PRESSED-ACC-FOLLOWUP.txt；本轮验收见
CANDIDATES-VALIDATION.txt。三个字段均为empirical_candidate，candidate=true，
vehicle_confirmed=false，未把旧标签的一致性升级为实际踏板/ACC保持语义已确认。
ACC enabled/available继续为None，不接入其相关位；speed也保持未知。
此前 ../offline-analysis/PEDAL-GEAR-FOLLOWUP.txt 解释了约1.45%的油门旧标签
残差，并记录用户停车挂R的补充回忆。首次版本及旧验收报告作为历史快照保留；
本轮修改前的源码副本位于results/songplus-readonly-candidates-20261018/before。
FIELD_EVIDENCE现已同步新候选及此前gas/gear补证，原有字节解码公式保持不变。

原始日志来自使用过旧 openpilot 的一次行程，不能当作原车纯被动基线。
本原型没有将任何条目提升为 spec/protocol-evidence.json 的实车已确认协议。

证据分层（FIELD_EVIDENCE 是机器可读的逐字段清单）

supported_encoding：原始字节到旧日志值的对应有充分支持，物理语义仍需独立标注。
  steeringAngleDeg  0x11F，LE signed16 x0.1 度；左右方向、零点及失效语义待核对。
  steeringTorqueEps 0x1FC，LE signed12 原始数值；不能认定单位为 Nm 或驾驶员力矩。
  brake             0x342 byte1 x0.01；仅为旧日志归一化量，不等于 brakePressed。
  left/rightBlinker 0x133 bit37/38；仍缺与实物灯同步标注。

empirical_candidate：保留候选值并明确限制。
  wheelSpeeds.fl/fr/rl/rr 与 vEgoRaw：0x1F0 的四个 12 位窗，比例 0.0725 km/h；
    轮位、完整位宽和真实速度标度未独立确认，vEgoRaw 只是四轮候选的算术平均。
  gearShifter：0x242 的 1=P、2=R、4=D 复现旧标签；末段约22秒R后转P静止，
    与用户补充的停车挂R回忆相容。仍缺仪表同步标注；3不擅自标为N，未观察枚举
    返回None / unsupported_enum。开头R缺少挂入前上下文，单独保留。
  gas：0x342 byte0 x0.01。raw29/58对应旧gas0.28/0.57，完整解释原约1.45%残差；
    固定binary64乘法后截断再保存Float32的公式在timestamp-only诊断中逐位重现
    全部698,825个有效旧值。它不确定旧源码身份或物理标度；原型继续直接解码，
    不复制旧数值截断模式。
  gasPressed：0x342的独立GAS_RAW byte0 >3，直接比较原始值而非旧gas标签。
    未证明4%的真实踏板行程，双踏板优先级/油门覆盖语义仍未知。
  brakePressed：0x123 bit43（byte5 bit3），独立于0x342刹车量。
    使用日志支持的checksum/counter候选；驾驶员踩踏与自动制动/保持来源待标注。
  cruiseState.standstill：0x220 bit8（byte1 bit0），独立于车速为0/车辆standstill。
    无已确认checksum/counter，不添加推测性完整性检查；实际停车保持含义待标注。

unconfirmed（当前运行原型）：一律 None，不把旧日志中的恒定 false/0 当成已确认。
  驾驶员力矩、steeringPressed、转向速率/故障、
  cruiseState enabled/available/speed、门/安全带、盲区、ESP、
  滤波 vEgo/aEgo、仪表速度、standstill 等。未列出的官方 schema 字段同样未知。

数值有效性与证据等级是两条独立维度
Snapshot.values 是主要接口，Snapshot.quality 是逐字段即时质量：
  available / unseen / stale / invalid_event / bad_length / bad_checksum /
  counter_rejected / unsupported_enum / unconfirmed。
available 只说明这条候选编码有新鲜、通过已实现检查的观测，不代表已确认物理语义。
diagnostics.messages逐报文列出checksum_check_enabled、counter_check_enabled和
integrity_evidence；0x220两种完整性检查均为false。它的available不意味着校验和
或计数器已经通过验证。三个新候选均须有各自新鲜、可接受的报文才输出bool，
缺报文/坏帧/拒收/超时输出None，不能由其他踏板值或速度补成false。
Snapshot.age_ns 保留最近成功解码的年龄；坏帧后隐藏值，直到下次成功接收。

仅接收原始 src 恰好为 0 的帧，不清除高位、不混入 src2/128/192/197。
0x11F 要求 DLC=5，其余七种要求 DLC=8；先检查 Event.valid 和完整长度。
0x123、0x1F0、0x1FC、0x342 使用官方 BYD checksum，8 字节和 mod256=255。
0x123的checksum及byte6低4位counter为该行程支持的候选规则，未验证错误帧下
车辆的失效语义。0x11F四位checksum、0x133/0x242/0x220 checksum均未确认。
counter 使用官方容忍规则（连续累计失败达到5才拒收），保留跳步统计；单次跳步
不自动等同于错误解码或 ECU 故障。0x242/0x220 未实现计数器规则。
超时取当前官方解析器的10个报文周期：角度/0x123刹车按下100ms，转向灯500ms，
其他200ms。这与前轮探索统一150ms窗口不同，回放单列真正的不可用样本。
这些是本地观测的新鲜度阈值，未经车辆安全验收。

官方 CarState 投影
snapshot.to_opendbc() 返回真正的目标 structs.CarState，但 canValid 永远为 false，
表示原型不完整、不可作为控制输入；它不是“本条 CAN 接收故障”的结论。
官方 schema 不能表达 nullable 布尔/数值，因此默认0/false必须配合 nullable sidecar
解释。gas/brake 在目标 schema 已移至 deprecated group，本原型仅保留在 sidecar。
没有通过固定10ms滤波器推导 vEgo/aEgo，也没有按回放调用次数推导驾驶员介入。

回放方法与来源
输入为 route 0000026f--584c284cef 的全部98段 rlog，以及之前严格解压/Cap'n Proto
校验后生成的逐帧缓存。每次回放重算98个原始 rlog和98个缓存的SHA256，并核验
schema、提取器、完整性报告及候选报告的来源链。本轮复用已验证的提取结果，
不是重新解压一遍 rlog，也不运行旧车端解码模块。

保持分段顺序、Event序号及帧顺序，不插值、不重排。对每个旧carState，只消费
文件顺序在它之前、且 logMonoTime不晚于它的CAN。跨段保留待消费CAN，最后尾帧
仍全部读完；sendcan只计数排除。只有旧Event.valid且旧canValid为真才参与对照。
对照差异可能包含异步发布时间的影响；不以调时间窗口强行提高一致率。

离线执行（仓库根目录；无需联网准备）
  /usr/bin/sandbox-exec -f tools/offline-network.sb \
    upstream-openpilot/.venv/bin/python -B -m unittest discover -s songplus_readonly/tests -v

  /usr/bin/sandbox-exec -f tools/offline-network.sb \
    upstream-openpilot/.venv/bin/python -B -m songplus_readonly.replay \
    --analysis-manifest results/rlog-analysis-20261018/analysis-manifest.json \
    --candidate-research-manifest results/songplus-pressed-acc-20261018/verification.json \
    --historical-replay-source results/songplus-readonly-candidates-20261018/before/songplus_readonly/replay.py \
    --out results/songplus-readonly-candidates-repeat.json

报告路径已存在时会拒绝覆盖；复验时指定新的输出文件。缺少本地环境或日志则失败，
不会自动下载或尝试连接车机。工具源码、DBC、测试及来源哈希会写入回放报告。
candidate-research-manifest只用于离线runner核验候选选择/历史留出来源，CarState
运行本身不加载研究脚本/日志/旧车型实现；本轮全程回放是实现验证，不是新的独立
留出样本。历史verifier通过修改前的源码副本校验，不强迫旧报告匹配新源码。
初版历史结果见VALIDATION.txt和results/songplus-readonly-20261018/；新候选结果见
CANDIDATES-VALIDATION.txt和results/songplus-readonly-candidates-20261018/。

下一批需要补的证据（这里只记录缺口，不执行车辆采集）
1. 同步标注方向盘回正/左/右、实际轮位和独立速度；确认角度与速度物理标度。
2. 仪表 P/R/N/D 与原始位同步，补精确换挡/运动方向标注及 N 正例。
3. 区分驾驶员踩踏与 ACC 制动，补松/踩油门、刹车退出和手动接管标注。
4. 补 ACC 可用/激活/暂停/退出按键的同步证据；现有官方 ACC 位不能直接沿用。
5. 补缺失的 checksum、ECU 身份、总线/线束物理对应和失效状态语义。
这些完成前，保留原车 ACC 与自主纵向仍是后续不同的协议/安全验收工作。
