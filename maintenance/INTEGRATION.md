# P7 之后的第一项：只读车辆与运行接入离线工程（P8）

本目录把用户要求的下一阶段第 1 项记为 P8，方便追加补丁与验收编号。
它不重定义此前 P0–P7，也不是安装或实车验证阶段。四个官方提交保持不变。

`downstream-lock.json` 保留原 P7 契约；`integration-lock.json` 绑定其完整哈希，
再固定追加 host/Panda 补丁、只读车辆源码与证据、验收程序和 CI。历史补丁及
报告保持原样。新补丁在显式下游分支维护，干净重放时先检查 P7 树，再追加 P8。
`songplus_readonly/evidence-lock.json` 进一步分开运行时源码、离线测试与外部
研究材料。原始日志、缓存、完整旧系统 schema/恢复材料不进入源码包或 CI 上传。

## 入口

在工程根目录，使用已经准备好的固定 Python 与本地 Linux VM：

```sh
# 可在没有官方 checkout、没有私有日志的源码 CI 中执行
python3 tools/songplus_integration.py verify-lock --metadata-only

# 验证官方源码和缓存依赖，缺失直接失败
upstream-openpilot/.venv/bin/python tools/songplus_integration.py verify-lock

# 新建目录中重放 P7 + P8；源码内容包不含安装器/原始记录
upstream-openpilot/.venv/bin/python tools/songplus_integration.py replay \
  --output results/p8-replay-001

# 共享 CI 完整入口；VM 参数只指向已准备的本地构建环境
upstream-openpilot/.venv/bin/python tools/check_songplus_integration_ci.py \
  --mode full --linux-instance byd-recovery-311 \
  --linux-root /home/shuang.guest/songplus-p2-build \
  --output results/p8-full-001

# 必须保留同批次子报告、日志和产物；通过但不可部署时返回 3
upstream-openpilot/.venv/bin/python tools/songplus_integration.py release-check \
  --report results/p8-full-001/validation/result.json
```

每次使用新目录。验收不修改锁、不下载依赖、不启动 VM 或设备服务。
完整运行要求锁及其源码已提交，未纳管的其他用户资料不自动进入提交或源码包。
外部日志沿用现有归档的来源和路径，仅有 Git 仓库不能完成 98 段真实数据回放。
缺这些数据时，完整验收失败；源码 CI 通过不冒充完整回放通过。
Python 解释器及 cffi、pycparser、pycapnp、NumPy 的既有安装文件也逐字节锁定，
与 P7 的官方提交、uv 锁和本地编译依赖锁一同检查；不以重装依赖自动消除差异。

## 本阶段验收

| ID | 必须执行的范围 |
| --- | --- |
| B80 | P7 与追加锁、干净补丁重放、两个目录的确定性源码包 |
| B81 | 只读 DBC/CarState 的 55 项测试、32 字段证据和 nullable 语义 |
| B82 | 98 段原始 rlog/缓存重新核哈希、完整因果回放，无 CAN Event 遗漏 |
| B83 | 新运行接入在 macOS/Linux 实际编译和注入 I/O 执行；旧 P6、SPI 回归 |
| B84 | 资源修复后的 F4/H7 debug/release 双路径 ARM 构建、资源与协议/safety 回归 |
| B85 | 链接越界/重叠/坏栈证据拒绝，以及缺少运行证明时明确拒绝放行 |
| B86 | 共享源码 CI、证据完整性与发布拒绝；同批次新执行的 P0–P7 41 项前置验收 |

各项记录实际执行组数、来源与哈希；统计有包含关系，不相加称为独立测试总数。
历史日志里旧软件的 carState 只是对照标签，全部字段继续未获独立实车确认。
未知 ACC 字段仍为 None，官方投影 canValid 仍为 false；没有新增车型注册、
CarController、车辆发送报文或宋 PLUS 专用 safety 放行。

## 实际运行接入的边界

新代码把完整发现结果、精确身份校验、USB/SPI 工厂、连接生命周期与现有
PandadRuntime/PandadLoop 接起来，并增加真实 cereal/msgq API 的消息边界。
本阶段只有注入的发现/I/O provider 和合成 profile；没有实物枚举器、实际身份
认证、设备打开或生产启动入口。Linux 在私有 `/dev/shm` 中实际创建 msgq
发布/订阅端点，验证真实 cereal 消息收发及 epoch 清队列；网络与设备节点保持
隔离。目标机端点、相机/推理及设备时延仍需后续工作，不能把宿主测试等同于
已经完成 C3XL 集成。10 ms 是循环内协作式预算，不包括外部发现、打开和 claim。

## 资源修复与保留门槛

H7 应用专用 linker 保留原 cookie ABI 地址，分开 DMA 音频缓冲与 cookie，
同时显式保留栈和签名尾部，并增加可失败的边界检查。原 bootstub linker 不变，
不构建或替换 bootstub。F4/H7 容量仍是参考构建预算，不能代表未知实物料号。
有界直接调用链、未解析间接调用和中断入口分别报告；最坏中断嵌套、汇编/FPU
现场与真实栈水位没有被假定完成。`--require-runtime-proof` 的预期拒绝是门槛
测试通过，不是运行资源安全通过。

部署仍受实物身份、恢复/信任链、目标机运行、驾驶员监控、声音告警、车辆
控制/safety 及完整资源证明阻断。固定的拒绝权限不能由填写 verified 或修改
一个配置开关放行。缺驾驶员相机的产品差异不会被关闭监控检查掩盖。

## CI

`.github/workflows/songplus-integration-offline.yml` 使用与 P7 相同的固定 checkout
提交、只读权限和隔离方式。普通 PR/分支任务只跑无私有数据的源码契约；完整
运行仅限显式手动触发、受保护的专用 macOS runner 和既有 Lima VM，且与 P7
共享并发组。测试/编译工人禁止网络、设备节点和 macOS IOKit；仅本地 VM 编排
需要沙箱外 IPC。没有发布、推送、上传或远程构建依赖下载步骤。

工作流配置入库与本地运行共享入口，不代表 GitHub Actions 已远端运行；
专用 runner/保护环境配置仍遵循 P7 的 `CI.md`。当前候选始终不可部署。
