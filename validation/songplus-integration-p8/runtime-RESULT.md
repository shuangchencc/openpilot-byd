# P8 运行接入：离线验收

状态：**PASS_OFFLINE_HOST_ONLY**；不具备安装或实车运行资格。
本报告记录统一批次 `results/songplus-integration-p8-full-002/validation/runtime` 中重新执行的 runtime 验收；完整机器记录位于该目录
`result.json`、`macos/result.json` 和 `linux/result.json`，简要归档见
[runtime-result.json](runtime-result.json)。此前失败批次和开发快验不计入本次通过数。

## 独立补丁与实际接入

Host 提交 `46baabd4f71a876d22ad61ab33bd9a873a194ebf` 追加在 P6
`d4d610ecb08ab27200e48c07757b147861f710a5` 之后。补丁和差量文件哈希位于
[P8 manifest](../../patches/openpilot/p8/manifest.json)，四个官方提交与 P0–P7 历史输入保持不变。

`RuntimeIntegration` 将发现快照、精确 profile 身份、连接 nonce/epoch 与现有
`PandadRuntime`/`PandadLoop` 串行 owner 接通；USB/SPI 工厂直接使用 P2 后端。
缺板、多板、错身份、重复句柄/过期快照、复用连接以及同 handle 激活证据撤销均关闭整组。
部分打开失败消耗两板 nonce，重连需要双板新句柄，撤销先取消 transport 后释放 owner。

`RealtimeSource`/`RealtimeSink` 使用未修改的真实 cereal schema，并接到实际
`SubSocket`/`PubSocket` 接口。四类输入执行 topic/长度/时间戳与 epoch 检查；队列 fence
有上限，CarParams 必须显式重新提交，发布失败关闭组。该接入候选为 noOutput，任何
sendcan 或 enabled 输入均在 transport 前拒绝。没有为用户实物猜测 profile，也没有
native 设备发现/I/O provider；当前只接受明确的 synthetic-bound fixture。

修复本轮实际接入暴露的接收缺陷：普通 noOutput/disabled 发送尝试只清 TX，保留 RX
队列和分片；故障、断板、epoch、更换配置与退出仍全清。另补齐旧相机样本的控制 epoch
起始时间下界，以及 same nonce 激活证据撤销检查。SConscript 的 vendored libusb
路径和链接依赖仅加到 daemon，原 extras 目标保持原库集。

## 本次实际执行

| 验收 | macOS arm64 | Linux aarch64 VM |
|---|---:|---:|
| P8 owner/transport/cereal 组 | 28，1903 断言 | 30，1937 断言 |
| 原 P0–P6 native 组（应用 P8 后重编译） | 168 + 5 SPI golden vectors | 168 + 5 SPI golden vectors |
| 原真实 cereal runtime 组 | 22，2137 断言 | 22，2137 断言 |
| 原 owner loop 组 | 10，822 断言 | 10，822 断言 |
| 原 P6 双板复合故障组 | 22，3138 断言 | 22，3138 断言 |
| SConscript 惰性构建契约 | 2 | 2 |
| 新真实 main 的 UNBOUND owner 入口 | 平台不构建 daemon | 1 |

以上执行项零失败、零跳过。兼容层与测试启用严格警告、ASan、UBSan；官方 main 编译
保留上游 unused-parameter 警告，策略与已有 daemon worker 一致。SConscript 两项是
惰性解析契约，不声称完成全部 SCons 构建。Linux 实际重编译新增 main 并链接所有 P8
模块的 pandad，运行旧 `--compat-offline`、新 `--compat-runtime-offline`，并验证
`--compat-runtime`、`--compat-device` 均 exit 2。原默认 SPI 路径保留且未执行硬件打开。

Linux 除内存 fixture 外，在独立网络/挂载/用户 namespace 的私有 16 MiB `/dev/shm`
tmpfs 中建立 7 对真实 msgq 端点；同进程验证三类输出字节往返、deviceState 输入和
跨 epoch 清空。其余输入 topic 的消息/拒绝语义由内存 fixture 覆盖。没有网络 socket，
没有主机共享 shm 挂载，没有真实设备端点；这不证明跨进程负载、目标调度或实时性。

新测试还覆盖双板 wire→cereal 全局 bus 0/5、noOutput 多周期 RX 分片、重连丢弃旧片、
迟到旧相机、销毁顺序、发现异常、发布失败、超限输入和 128 项 nonce 历史上限。
旧回归保留拒绝后不得向本板或另一板继续发包、掉板/替板/重连、过期队列、退出、断电
模拟以及单 H7/SPI 的原屏障。

输入/依赖/工具/补丁和完整源树在运行前后重哈希；每条新命令记录 stdout/stderr 哈希，
每个 worker 对全部保留文件建立精确清单（Mac 277 项、Linux 324 项），
包括生成代码、旧回归二进制、日志、新 fixture 和 pandad-p8。统一门槛再独立核对这些字节。

## 失败记录与剩余门槛

独立批次 001 因新二进制缺少 pinned libusb rpath，在 Linux loader 阶段失败；加上明确
rpath 后批次 002 完成全部旧回归和新 fixture，但官方 main 的既有 unused 参数警告被
错误地升级为 -Werror 而失败。修正仅让官方 main 沿用旧 worker 的警告策略；新增兼容源
和测试继续 -Werror。失败记录保留为 `runtime-prior-failed*.json`，随后已有对象快验
仅标记 `PASS_DEVELOPMENT_SMOKE_ONLY`。本表全部来自修正工具后统一批次的完整重编译。

仍缺：真实板卡身份/精确 profile、native 发现与句柄取得、真实 USB/SPI/IPC 的目标时序、
设备上的系统/恢复与签名信任链、驾驶员相机硬件和 driver monitoring、物理双板故障、
CAN 信号确认与宋 PLUS 专用 safety。此 host 任务没有新 ARM 固件构建、MCU 执行、设备
访问、真实 CAN TX、系统/槽位修改、reset、安装或刷写。
