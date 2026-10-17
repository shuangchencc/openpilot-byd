# P4 独立补丁与离线复验

固定官方 openpilot `35e78a9d9518855ec0dacec919e719624a28131f`，依次重放
原 P0/P1/P2/P3 四份补丁，再应用 P4。旧补丁、旧报告、官方 gitlink 保留。
下游分支为 `songplus/dos-usb-p4`；提交、结果源码及依赖 SHA 见 manifest。

P4 将逐板控制会话、P2 模拟传输、P3 路由、cereal 输入/输出和有界事件循环
接入实际 pandad 的显式离线入口，同时修改四处消费者的显式 profile 分支。
默认单 SPI 设备路径保持原实现。兼容 profile 的普通启动会在硬件导入或
发现前拒绝；离线入口不会进入 Python 包装器的自动 reset/flash 路径。

所有绑定仍是 P0 合成身份、所有 safety 配置仍为 noOutput，真实设备、CAN
发送及固件安装权限仍关闭。该阶段的“接合成功”仅指 fake backend 的模拟
调度屏障；不能解释成 noOutput 固件允许实际控制，也没有宋 PLUS safety。

## 复验

从工程根目录运行：

```sh
upstream-openpilot/.venv/bin/python tools/validate_panda_control.py \
  --linux-instance byd-recovery-311 \
  --linux-root /home/shuang.guest/songplus-p2-build
```

指定的是本机 Lima Linux 构建 VM。P2 的 libusb 仍在原任务缓存；新增 daemon
依赖在 `host-dependencies.json` 中指定的 `/home/shuang.guest/songplus-p4-build/deps/prefix`。
验证器不启动/安装 VM，不联网下载或安装包。依赖缺失或 SHA 不符即失败。

新增 Linux capnproto/json11/zeromq/zstd 准备阶段使用官方基线 `uv.lock` 中
完全相同版本、URL 和 SHA 的 wheels，仅解包到本地/VM 的构建目录，没有
修改 VM 系统包。macOS 复用锁定 Python3.12.13 环境中的 Cap’n Proto、
pycapnp2.1.0 和原 P2 libusb。依赖准备与禁网、隔离设备节点的验收分开。

验证包括干净重放和文件集合核对，P0–P3 回归、P4 原生会话/循环/消息链、
固定 schema 的 Python 消费者，以及直接抽取锁定固件 C 函数的 liveness
oracle。Linux 编译并链接实际 `main.cc/pandad.cc/panda_safety.cc/Panda/SPI`、
common/msgq/cereal 和新增兼容层，只执行显式离线入口。macOS 原 SPI 源依赖
Linux spidev，因此只构建实际 cereal/runtime fixture，不声称完整 daemon。

消费者测试执行真实源码中选取的函数/分支；C++ 原生消息还由 pycapnp 解码
并交给实际消费者策略作跨语言对照。没有启动完整 selfdrived、hardwared、
card 或 UI 进程。固件 oracle 使用锁定的实际 C 函数；HAL、safety-hook 和无关驱动调用为明确
stub，不验证 MCU safety-hook 本体，也不是 MCU 固件构建或硬件 watchdog 验证。所有层次在机器结果中分别记录。

## 继续维护

源码契约见新增 `compatibility/P4-INTEGRATION.md`、`CONTROL-SESSION.md`。
为每次修改导出新的独立补丁、更新源文件/依赖/验收映射哈希，再重放验证；
不要改写历史补丁或更新官方 gitlink 来适应失败测试。当前没有安装入口。
P5 F4 BSP、P6 扩展故障与安全/目标机整合、P7 维护封装，以及实物恢复、
驾驶员监控和车辆协议/safety 验收仍未完成。
