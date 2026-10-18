# P10：持续发现、SPI 新鲜性与生产启动门槛

P10 是 P0–P9 之上的独立补丁；官方 openpilot、Panda、opendbc、msgq
和旧补丁/维护锁保持不变。`lock.json` 递归校验 P9 维护锁；完整验收重放
P6 汇总链 + P8 + P9 + P10，验证两份确定性源代码包，并新跑 Mac/Linux
主机测试。未改变的 ARM 固件构建、safety 与车辆回放仅核验保留的 P9/P8
前置证据，不计为本轮新执行。

## 实现边界

- `DiscoveryScheduler` 由串行 owner 每轮 tick；一次至多 start 或 poll，
  不 sleep。后台任务执行发现/重探测；主循环仍负责 health、heartbeat、退出。
  默认重探测间隔 50 ms、任务预算 100 ms、快照最长年龄 250 ms；重试从
  10 ms 指数退避至 500 ms。完整双板才可提交，逐板时间必须来自实际探测。
- 发现结果带 generation/revision；迟到任务、旧 nonce、时间回退、过期结果、
  半组、换板或任一板失败都会撤销整组。原句柄重探测不制造新 epoch；重新
  发现必须是两个新 lease。有限历史耗尽时停止，不能通过清空历史绕过。
  USB 重探测刷新 UID/板型/协议/固件签名/F4能力；serial 是同一 retained
  lease 初次读取的身份事实，本轮没有为旧 backend 增加 serial 再读取接口。
- 原生执行器将发现/探测/清理放在单个工作线程。取消是本地信号；阻塞调用
  未退出时保留该 lane，不再生成替代线程。backend 代理与重探测使用 try-lock。
  这不是内核硬期限证明：原生 open、enumeration、ioctl 和既有同步 runtime
  I/O 仍可能超时返回很晚，实机期限门槛仍未满足。
- SPI 使用同一 fd 的全 session advisory flock；先用不执行 endpoint 的无效
  帧排空旧 response，再连续完成两个不同 ID 的只读 UID 请求。基于锁定
  固件只缓存最近 `(request ID, request CRC)` 的逻辑，第二次 UID 必定新执行。
  后续同一 session 的 ID 单调递增，溢出前拒绝；随机起点或磁盘计数器不作为
  新鲜性证明。fork 子进程不能复用继承的 session；子进程析构跳过解锁，
  仅关闭其继承 fd，保留父进程锁。所有 master 必须遵守同一节点的锁，未知所有权默认拒绝。
- 测试直接编译锁定 Panda 的 `board/drivers/spi.h`，仅替代 DMA/endpoint
  外设接口，覆盖旧缓存、未读/半读响应、进程竞争/崩溃后锁回收等。
- 生产 gate 将“可供审核”与权限分开。没有真实 verifier、身份、恢复、拓扑、
  内核时间等证据时拒绝。所有权限字段始终 false。P10 的真实 pandad main
  默认在 Hardware/Panda 初始化之前返回 3；不能退回旧 SPI 分支绕过。
  `--compat-production-check-offline` 只输出 UNBOUND 诊断；没有生产启动器。

## 可复现入口

```sh
upstream-openpilot/.venv/bin/python -B tools/songplus_lifecycle.py verify-lock
upstream-openpilot/.venv/bin/python -B tools/songplus_lifecycle.py replay --output results/p10-replay-NEW
upstream-openpilot/.venv/bin/python -B tools/check_songplus_lifecycle_ci.py --mode full \
  --linux-instance byd-recovery-311 --linux-root /home/shuang.guest/songplus-p2-build \
  --output results/p10-full-NEW
upstream-openpilot/.venv/bin/python -B tools/songplus_lifecycle.py release-check \
  --report results/p10-full-NEW/validation/result.json
```

最后一条在证据完备但仍不可部署时按约定返回 **3**。校验不下载依赖、不访问
设备；只能使用预先准备的工具和本地 Linux VM。正式完整验收要求受保护文件
已提交且逐字匹配该提交，保留源文件、依赖哈希、命令、日志、二进制和子报告。
主机编译使用 ASan/UBSan。Linux 手工链接实际 main 和旧单 H7/SPI 实现，并
仅执行离线/拒绝分支；执行 SConscript 的惰性 build contract，不宣称运行了完整 SCons。

## 旧文档勘误

P9 文档中关于 `kLoopMaxGapUs` 的 10 ms 表述不准确：锁定代码实际为
**250,000 µs（250 ms）**。10 ms 不是快照寿命；P10 不修改该代码常量。
保留历史文档/锁不变，由本条补充更正。

真实 profile 仍使用 `profiles/unbound.json`。离线结果不补造板卡、USB alt、
SPI 节点、独占性、驾驶员摄像头或恢复资料；实机前置条件见 HARDWARE_GATES.md。
