# P9 原生 provider 的独立维护层

P9 追加于既有 P0–P8 之后。官方四个 pin、旧补丁、旧维护锁和旧 CI 保持不变。
`lock.json` 固定本层源码、测试、工具、patch、CI 及历史 P8 验收报告的哈希；
验证命令不会更新锁、下载依赖或探测设备。

```sh
upstream-openpilot/.venv/bin/python -B tools/songplus_native.py verify-lock
upstream-openpilot/.venv/bin/python -B tools/songplus_native.py replay --output results/p9-source-replay
upstream-openpilot/.venv/bin/python -B tools/check_songplus_native_ci.py --mode full \
  --linux-instance byd-recovery-311 --linux-root /home/shuang.guest/songplus-p2-build \
  --output results/p9-native-full
```

每次输出目录必须不存在。完整验收前，锁内文件必须与已提交的当前 Git commit 一致。
Mac 编译/测试使用 Seatbelt 禁止网络和设备访问；Linux 使用现有本地 Lima VM，
在独立用户、网络和挂载命名空间中执行，只提供私有 `/dev/shm`。
实际 libusb 与 Linux spidev 代码会编译并链接，测试全部注入调用，不枚举或打开宿主硬件。

本层新执行：两轮确定性 host 源码重放、源码 CI、两个平台的原生 USB/SPI/probe/profile
测试，以及同一 P9 源码上的既有 P8 runtime、P0–P6 native、双板、SPI 回归。
Linux 实际 pandad 额外链接全部新 provider，仅运行原有离线入口和拒绝参数测试。
默认 main、原 SPI 路径不通过此维护入口启动，不提供活动设备命令。

P8 `full-002` 的 ARM 构建、资源与 98 段车辆回放是**复核的历史前置证据**，
没有在 P9 计作新运行。release-check 重读原报告及其保留产物，并明确区分新测试与复用证据。

```sh
upstream-openpilot/.venv/bin/python -B tools/songplus_native.py release-check \
  --report results/p9-native-full/validation/result.json
```

有效离线证据仍返回 `BLOCKED_FOR_DEPLOYMENT`、退出码 3。真实身份未知时保持 UNBOUND；
合成 profile 通过测试不能构成实机 profile。真实板卡身份、完整固件与 ABI、USB 拓扑和接口、
SPI 节点/时钟/握手、接线与总线角色、内核权限、时序/断连、恢复链都需后续明确授权的实机核实。
完整栈上界、目标运行、驾驶员监控、宋 PLUS 控制器及专用 safety 的现有缺口继续阻断部署。

维护者在审查源码和测试变更后，才可显式运行
`python -B -m native_maintenance.refresh_lock --write-reviewed-lock`。该命令不会自动验收或发布。
源码 CI 可在不含官方检出、私有回放和缓存依赖的副本运行；完整验收需已有离线缓存和历史证据。
