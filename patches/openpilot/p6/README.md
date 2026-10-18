# P6 独立补丁与离线复验

P6 在原 P0–P4 host 补丁后新增第六份 host 补丁，同时引用独立的 P5 Panda
补丁。官方基线和旧阶段补丁/报告不变。完整提交、文件集合及依赖哈希见
[manifest.json](manifest.json)；逐项证据见 [验收报告](../../../validation/panda-compat-p6/RESULT.md)。
下游分支为 `songplus/dos-usb-p6`。

新增连接 epoch 绑定的 F4C1 能力握手、字段可用性、双板复合故障关闭、
MCU 拒绝来源诊断、退出与突然失联边界。所有实际配置仍只允许 noOutput；
“接合”和 CAN 发送正向测试仅在 fake backend 内进行，真实权限保持关闭。
默认官方单 H7/SPI 源文件不改，且不向 H7 发送 F4 的 0xd5 请求。

从工程根目录复验：

```sh
upstream-openpilot/.venv/bin/python tools/validate_panda_p6.py \
  --linux-instance byd-recovery-311 \
  --linux-root /home/shuang.guest/songplus-p2-build
```

这里的 Linux 是已准备好的本机 Lima VM。验证器不启动 VM、不安装依赖、不联网；
只使用 P4 锁定的 capnproto/json11/zeromq、P2 libusb 及本地编译器。macOS
Seatbelt 禁止网络、设备节点和 IOKit 打开；Linux 使用私有网络、用户和 mount
namespace，并提供空设备目录。整个流程不调用 Panda 构造器或实物入口。

正式验证重新从官方 Git 归档建立源码、依次应用补丁，校验完整 P5 源树和 host
文件集合。Linux 编译真实 pandad（含原 Panda/SPI），只执行 `--compat-offline`；
macOS 编译原生 cereal/runtime fixture。两端执行相同故障、能力、传输、SPI 和
实际 safety 源码 oracle，ASan/UBSan 开启。Python 使用锁定 3.12.13 环境。

P6 safety oracle 编译当前实际 opendbc hooks、P5 CAN 队列和协议代码，并逐字
抽取 set_safety_mode、tick、watchdog。HAL 使用 mock；F4/H7 是 host 编译策略，
不是本轮新 ARM 固件构建。P5 的四个 ARM 候选结果保持原历史报告。
精选官方 safety 49 项也重新编译运行，但不代表所有车型 safety 全套测试。

无真实设备发现/实时 PubSub 接入、部署包或刷写入口。F413/DOS 参考板型尚未实物
确认，恢复链、驾驶员监控、音频告警、宋 PLUS 控制器和专用 safety 等仍待验收。
P7 的维护封装和完整发布门槛不由本阶段通过自动完成。
