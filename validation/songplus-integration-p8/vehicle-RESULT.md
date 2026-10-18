# 宋 PLUS 只读成果维护纳管与离线复验

本轮已将现有只读原型、接收 DBC、字段证据分级、故障测试和回放来源纳入独立维护合同。已有解码器、DBC、回放器及原 55 项测试没有改动，候选语义没有提升；官方目标 opendbc 仍固定为 `b3bd79a659dae961af169a3baac723961f720133`。

新增 `maintenance.vehicle_readonly` 提供标准库源码合同检查和完整本地验收入口。源码合同不需要 opendbc、虚拟环境或私有日志，可供公开 Linux CI 使用；完整回放依赖既有本地数据，缺依赖或散列不一致直接失败，不下载、不连接设备，也不以跳过代替通过。

## 新鲜执行结果

正式批次：`results/songplus-integration-p8-vehicle-frozen-20261018/acceptance.json`，SHA256 为 `eb4d4b0799417fa4e97fb4698280ecaf18bc0ca3b6fcac21d50493f3621d8b2b`。

- 新增维护合同/篡改拒绝测试：34 passed，0 failed，0 errors，0 skipped。
- 原型已有测试：55 passed，0 failed，0 errors，0 skipped，记录全部唯一测试 ID。
- 98/98 原始 rlog、98/98 缓存重算 SHA256；原日志共 1,091,731,052 字节。
- 699,380 个原 CAN Event 全覆盖，无遗漏；只使用 raw src0，sendcan 仅统计排除。
- 32 个字段的历史比较及质量统计与锁定参考完全一致。
- 698,957 次 ACC 未知检查保持 `None/unconfirmed`；3,780 次官方投影检查保持 `canValid=false` 和 nullable sidecar。
- 解码器、回放器、DBC、维护 validator 及沙箱配置均记录来源哈希；validator 和原型运行首尾未漂移。
- worker 均在 `panda_firmware_offline.sb` 中运行，禁止网络、设备节点及 IOKit 打开；正式回放耗时约 129.3 秒。

机器可读摘要见 `vehicle-evidence.json`，精确版本化文件清单见 `vehicle-versioned-files.json`。首个先行功能批次保留在 `results/songplus-integration-p8-vehicle-20261018/`；由于它早于最终 validator 冻结，不作为最终维护合同证据。上述正式冻结批次才用于本报告。

## 维护与资料边界

`songplus_readonly/evidence-lock.json` 固定 13 个源码/DBC/测试/说明文件，另由外层维护锁绑定该证据锁及 validator。运行所需仅四个本地文件：`__init__.py`、`runtime.py`、`carstate.py` 和接收 DBC。`field-evidence.json` 为 32 字段的审计快照，原型测试核对其与 `FIELD_EVIDENCE` 一致。

原始 rlog、npz、完整旧 schema、历史大研究 JSON 和导出内容仍在既有 ignored/local 位置。这里只固定 98 段来源摘要、18 个外部报告/脚本的散列与旧 schema 摘要；没有复制原始日志、私有车机材料、设备地址或绝对外部路径进入版本化证据锁。只读解码器不加载这些研究材料。

完整回放当前明确限定已准备的 macOS 环境，旧 manifest 仍使用其原绝对路径。源码 CI 的通过不能代替完整私有行程回放，也不能宣称已经执行 Linux 上的 98 段回放。

本轮复用经哈希核对的旧严格解压/Cap'n Proto 提取结果，没有重新提取原始日志。行程来自旧 openpilot 使用记录，旧 carState 是历史比较标签，既不是原车纯被动基线，也不是独立物理真值。5 项 supported_encoding、10 项 empirical_candidate、17 项 unconfirmed 保持原级别，实车独立确认仍为 0。

本报告不涉及 MCU 构建、C3XL 相机/系统运行、真实总线、控制器或 safety 实车验证；这些属于外层独立补丁或后续实机门槛。没有部署、系统写入、重置或刷写权限。

## 可调用入口

```text
python3 -B -m maintenance.vehicle_readonly source-check --out <新JSON路径>
python3 -B -m unittest maintenance.test_vehicle_readonly -v
python3 -B -m maintenance.vehicle_readonly acceptance --out <新结果目录>
```

Python 接口：`verify_contract(root=ROOT)`、`acceptance(output, ...)`、`validate_acceptance_report(report, output, root=ROOT)`。最后一个只核对已保留证据，不重新执行测试；外层验收必须调用 `acceptance` 形成新批次。已存在输出、符号链接路径、重复 JSON 键、非有限数字、未知源文件、来源/字段/计数漂移均拒绝。
