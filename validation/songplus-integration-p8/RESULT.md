# P8：宋 PLUS 只读成果与运行接入离线集成

状态：**PASS_OFFLINE_INTEGRATION**。本轮对应 P7 后第 1 项离线工作，
全部 B80–B86 通过，同批次重新执行 P0–P7 的 41 项前置验收。
发布核验仍为 **BLOCKED_FOR_DEPLOYMENT**，退出码 3；不能安装或用于实车控制。

本批次维护源码提交：`674721a0408e2a0153757a6eba569247f4784b08`。
新增 host 补丁：`46baabd4f71a876d22ad61ab33bd9a873a194ebf`；
新增 Panda 补丁：`c2aa3df08fe4864c9e4ecb4b4bbd150117ce2397`。
四个官方基线、P0–P7 补丁、原维护锁和历史报告保持不变。

## 实现内容

- 只读原型、接收 DBC、32 字段证据和 55 项已有测试已纳管。没有改动原型解码器、
  DBC 或提升候选字段的可信度。98 段日志及缓存只锁定来源哈希，原始记录和完整
  旧系统 schema 不进入 Git、源码包或 CI 上传。
- 新增独立维护锁，绑定 P7 锁与 40 个追加文件；补充 968 个 Python 依赖文件的
  字节锁、454 个官方 opendbc 源文件清单，以及完整测试 ID 库存。
- 运行接入新增完整发现快照、精确身份/角色绑定、USB/SPI 对象生命周期、实际
  cereal/msgq 消息适配和有界 epoch 队列清理。Linux 在私有共享内存中创建七对
  真实发布/订阅端点，实际负载覆盖三个输出 topic 和 deviceState 输入；其余
  输入边界由注入测试覆盖。实际 pandad 编译、链接并执行未绑定的显式离线入口。
- 修复只读禁发分支误清 RX/分片、旧相机消息跨控制 epoch、相同 handle 的激活
  证据变化漏检，以及 extras 目标错误继承 libusb 依赖。故障、掉板、epoch 和
  MCU 拒绝仍清全部队列；拒绝后没有绕板发送路径。
- 修复 H7 应用 cookie 与麦克风缓冲重叠；明确预留 cookie、栈、heap 和签名
  尾部，并加入链接边界与栈证据拒绝测试。使用应用专用 linker，原 bootstub
  和 generic SCons 固件路径未修改或宣称验证完成。

## 同批次验收

正式目录：`results/songplus-integration-p8-full-002/validation/`。
UTC 时间：`2026-10-18T05:26:00.812267+00:00` 至 `2026-10-18T05:35:28.760781+00:00`。

| 范围 | 实际结果 |
| --- | --- |
| B80 维护与重放 | 两个新目录重放；host 294、Panda 235、opendbc 454、vehicle 14 个文件；源码包相同 |
| B81 只读原型 | 55/55，32 字段；5 编码支持、10 经验候选、17 未确认，实车独立确认仍为 0 |
| B82 完整回放 | 98/98 原始日志与缓存重算哈希；699,380 个 CAN Event，遗漏 0；3,780 次投影及 698,957 次未知 ACC 检查 |
| B83 运行接入 | macOS 28 组、Linux 30 组；Linux 实际 main 1 组；双平台保留 P6/SPI 回归；ASan/UBSan |
| B84 候选资源 | F4/H7 × debug/release × 两轮，共 8 次 ARM 构建；raw/signed/ELF 共 12 对逐字节相同 |
| B85 拒绝门槛 | 23 项资源检查；协议 30 组、6 个旧 ABI 负编译、bxCAN 24、FDCAN 14、BSP 23、能力 12、精选 safety 49、heartbeat/watchdog 26 |
| B86 CI 与旧回归 | macOS/Linux 源码 CI 各 214 项维护测试 + 7 项 CI 测试；同批次 P0–P7 41 条件全部通过 |

所有正式验收均为 0 失败、0 跳过。统计有包含关系，不合计为独立测试总数。
完整回放在 macOS 读取现有本地归档；Linux 源码 CI 不含官方 checkout 或私有日志，
未声称在 Linux 执行完整行程回放。旧 carState 仅作历史标签，不是物理真值。
源码包 SHA256：`200fe997539d893806e2dbed118a12870664b5d56fe3daef3c0c1795d769318b`。
维护锁 SHA256：`1ee1afdd5a7931128f1f3aaaca1facdef4570858ea1dcb1815556f4c024d2838`。

## 保留的实际运行缺口

本轮没有原生设备枚举/I/O provider、真实 profile 或生产启动入口；USB/SPI 仍使用
注入的设备 I/O。真实 msgq 测试只在 Linux 私有 tmpfs 的同一进程中执行，未验证
跨进程调度和并发，不能证明 C3XL IPC、
相机、推理、温度或端到端时延。循环 10 ms 是协作式预算，不涵盖外部发现/open/claim。
只读原型继续 `canValid=false`，未知字段保持 nullable；没有注册可接合车型、
新增 CarController 或宋 PLUS 专用 safety。

H7 1024 B、F4 8192 B 是候选栈保留区；部分已知直接链的通过不构成完整最坏栈
证明。间接调用、汇编/FPU 现场、中断嵌套、真实 MCU 容量和栈水位仍未验证。
`--require-runtime-proof` 仍明确返回拒绝；不能用一个配置开关放行。
实物身份/线束、恢复与签名信任链、目标运行、驾驶员监控、声音告警、车辆控制
和独立 safety 均继续设门槛。缺驾驶员相机没有通过关闭监控来绕过。

## 过程记录与复验

开发阶段保留了 venv 路径、未解析 IRQ 入口、libusb rpath 和官方源码警告处理等
失败记录。预验收 runtime-002 暴露官方 HardwareNone 警告被升级为错误后，
停止了 full-001，修正 worker、重新锁定提交，再从头执行 full-002。
新增兼容代码继续启用 `-Werror`；官方 main 保留原有警告。失败、取消及 smoke
结果均不计入本次正式通过。单项先行报告仍保留；本文件以统一 full-002 为最终批次。

```sh
upstream-openpilot/.venv/bin/python -B tools/check_songplus_integration_ci.py \
  --mode full --linux-instance byd-recovery-311 \
  --linux-root /home/shuang.guest/songplus-p2-build \
  --output results/p8-full-NEW

upstream-openpilot/.venv/bin/python -B tools/songplus_integration.py release-check \
  --report results/songplus-integration-p8-full-002/validation/result.json
```

发布核验须保留整个正式结果目录；此处复制的 JSON 是审计摘要，不可替代原产物。
`evidence-index.json` 记录原子报告路径和散列。CI 配置与共享入口已本地验收，
没有推送、远程 GitHub Actions 执行或发布。所有目标设备操作保持关闭：未连接
C3XL、未修改系统/切槽、未重置或刷写 Panda、未发送真实 CAN、未执行 MCU 固件。
