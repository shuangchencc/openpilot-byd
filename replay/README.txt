CAN 日志只读验证与统计

用途
本目录是宋 PLUS 独立适配研究的输入检查层。只读取本地规范 JSONL 和 manifest，
输出 JSON 统计；不连接设备、不使用 socket/SSH、不发送 CAN、不生成控制指令或 CarState。
仅依赖 Python 标准库，Python 3.10+。车型协议证据与可选 DBC 解码由其它组件负责。

运行
python3 /Users/shuang/Code/openpilot/songplus-port-lab/replay/canlog.py validate --manifest /Users/shuang/Code/openpilot/songplus-port-lab/replay/fixtures/synthetic-can-data.manifest.json
python3 /Users/shuang/Code/openpilot/songplus-port-lab/replay/canlog.py summarize --manifest /Users/shuang/Code/openpilot/songplus-port-lab/replay/fixtures/synthetic-can-data.manifest.json

成功：exit 0，stdout 为完整 JSON 报告。
失败：exit 2，stderr 为 validation=failed 的 JSON，不输出部分成功报告。
argparse 的命令行用法错误同样返回非零，但使用标准 argparse 错误文本。
工具不写文件；需要保存报告时由调用者明确重定向 stdout。

JSONL 合约
每行一个对象，字段必须恰好为：
{"timestamp_ns":0,"bus":0,"address":291,"data":"00010203"}

- timestamp_ns：0..2^63-1 的整数纳秒，来自同一单调时钟；整份文件按非递减顺序排列。
  相同时间允许且保留；倒退失败。不会自动排序、改时钟、插补或丢弃帧。
- bus：0..255 整数，按原数字分别统计，不自动映射端口、去掉回环标志或合并总线。
- address：0..0x1FFFFFFF 的 JSON 整数。十六进制字符串不接受。
  该格式只保存数值 ID，不保存 IDE/RTR/error flags；不是无损替代所有硬件原始格式。
- data：连续 ASCII 十六进制，每字节两位，可大小写，不允许 0x、空格、下划线。
  允许空 payload。合法长度为 0..8、12、16、20、24、32、48、64 字节。
  长度>8表明需要CAN-FD；长度<=8不能判断原帧是classic CAN还是CAN-FD。
- 本工具只接收已规范化的数据帧，不接收 candump、任意二进制日志、远程请求帧标志或错误帧。
  来源格式如何规范化需要另建有证据的转换器，不能删除标志后直接宣称无损。
- 未知/缺失/重复字段、非有限 JSON 数值、坏 UTF-8、空行、空日志、非法长度均失败。
  单行上限65536字节；不会执行记录内容。

必需 manifest 合约
{
  "schema": "can-jsonl-manifest/v1",
  "data_kind": "synthetic",
  "log_file": "synthetic-can-data.jsonl",
  "sha256": "该JSONL原始字节的64位小写SHA256",
  "timestamp_unit": "ns",
  "timestamp_clock": "monotonic",
  "description": "来源和用途的简短说明"
}

字段同样不可增减。data_kind只能是synthetic或recorded，不接受unknown或缺省。
recorded是manifest的来源声明；工具不会因为这个标签就认定内容来自实车。
SHA256只证明本次读入字节与manifest匹配，不证明采集来源、车型身份或字段正确。
log_file是相对manifest所在目录的规范POSIX路径；禁止绝对路径、../、反斜线、控制字符、
通过符号链接逃出该目录。manifest本身上限65536字节。
description仅用于报告展示，不作为指令或代码执行。

统计含义
- 按(bus,address)分别输出帧数、长度分布、首末时间、间隔最小/最大/均值、零间隔数。
- observed_interval_rate_hz = (帧数-1) / 首末时间跨度（秒）。
  单帧为null；多帧但时间跨度为0也为null；含重复时间但跨度>0时给数值并显式标记。
  这是记录中的观测频率，不证明真实发送周期；缓冲、丢帧、时间分辨率都会影响它。
- raw_byte_candidates只记录每字节出现值、相邻同总线同ID帧中的字节变化/bit翻转。
  payload变短时缺失字节不会跨过中间帧继续比较。
  不自动断言速度、转角、制动、校验、counter、符号、大小端、比例或单位。
- short payload可能来自classic CAN或CAN-FD，不擅自补足frame类型。
- 所有聚合数据只在整份日志验证及SHA256匹配后返回。

Python接口
从songplus-port-lab作为包根导入：
from replay.canlog import load_recording, Frame, ValidationError, analyze

manifest, frames = load_recording(manifest_path)

返回manifest原字段dict与不可变tuple[Frame,...]。
Frame为frozen dataclass，字段timestamp_ns:int、bus:int、address:int、data:bytes。
load_recording完整验证后才返回，输入或文件访问失败抛ValidationError。
此接口将帧物化到内存，适合需要按时序交给可选纯离线解码器的调用方。
只需统计大日志时使用analyze(manifest_path, summarize=True)，它逐行聚合，
内存主要随不同(bus,ID)数及每条消息的最多64字节统计增长。
analyze(..., summarize=False)只验证输入和来源绑定，不保存报文字节候选。
analyze的文件访问异常可能为OSError；CLI已统一转成非零错误响应。

测试与样例
python3 -m unittest discover -s /Users/shuang/Code/openpilot/songplus-port-lab/replay/tests -v

fixtures/synthetic-can-data.jsonl是人为构造的10帧：3条数字总线、4个(bus,ID)组合，
含重复时间、CAN-FD长度变化和一个29bit范围数值ID。没有真实车辆或个人数据；
这些字节不对应任何已确认物理信号。不要拿它为本车适配结论提供证据。
tests/test_canlog.py的临时数据也全部合成，并在本目录下建立及清理。
本轮26项unittest通过；详细范围见tests/test_canlog.py和test-results.json。

独立审查边界
没有安装候选控制栈、执行车型controller、导入Panda或调用硬件API。
官方DBC/CANParser的可选解码必须在本验证层之后接入；即便解码成功，
只能称“按该DBC候选解释”，不能自动升级为本车协议证据或控制许可。
