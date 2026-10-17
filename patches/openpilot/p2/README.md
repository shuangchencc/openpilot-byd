# P2 独立补丁与离线验收

基线仍为 openpilot `35e78a9d9518855ec0dacec919e719624a28131f`，
panda `a59195163c32a3a375ea6c86715dc08fd0984c1b`，opendbc
`b3bd79a659dae961af169a3baac723961f720133`；不更新官方子模块。

按 manifest.json 的 series 顺序应用 P0、P1、P2；前两份补丁引用原目录，
不复制或重写。所有变更限于 `openpilot/selfdrive/pandad/compatibility/`。
开发分支为 `songplus/dos-usb-p2`；补丁和完整结果文件均记录 SHA256。
原 `panda.cc`、`panda_comms.h`、`spi.cc`、`pandad.cc` 和 `SConscript`
必须与锁定基线一致，验收器会检查。

## 复验

```sh
upstream-openpilot/.venv/bin/python tools/validate_panda_transport.py \
  --linux-instance byd-recovery-311 \
  --linux-root /home/shuang.guest/songplus-p2-build
```

这里的 Lima 实例是本机 Linux 构建环境，不是 C3XL。需先自行准备运行中的
Linux VM、g++、zlib 开发文件和 libusb；验收不会启动/安装 VM、联网下载或
安装缺失依赖。`host-dependencies.json` 固定本次 Mac 已有 libusb 和 Linux
任务目录解包后的 libusb 文件。Linux 包通过显式准备步骤下载后使用
`dpkg-deb -x` 解包，没有安装到系统；版本/哈希与来源均记录。

验收在新临时目录解包官方源码，依次应用补丁，核对全部源码 SHA，再执行
P0/P1 回归以及 P2 主机测试。macOS 使用 Seatbelt 禁网和设备节点访问；
Linux 使用私有 user/network/mount namespace，只暴露 null/random 节点。
VM 输入 tar 在传输后核验哈希。生产 libusb 与 Linux spidev 后端实际编译链接，
测试仅使用模拟 I/O；原 Panda 构造器、USB 枚举和 spidev 打开均不执行。

`run_panda_transport_host.py` 是两平台共同的独立传输构建目标。
它不等于完整 pandad/SCons 构建，也不等于目标 AGNOS、MCU 编译或设备验收。
所有新 C++ 测试开启 ASan/UBSan，失败、缺依赖、缺测试证据均不能标记 PASS。
结果保存在忽略目录 `results/panda-compat-p2/`；版本化摘要在
`validation/panda-compat-p2/`。保留旧 P0/P1 报告，原设计 NOT_RUN 是历史快照。

## 维护与边界

维护时从固定 P1 提交创建/继续下游分支，提交经过审查的 P2 变更，使用
`git format-patch --stdout <P1>..HEAD` 更新补丁，再更新 manifest 的提交、
补丁哈希、结果文件哈希和验收映射，最后运行上述干净重放验收。
不可只在开发目录编译后声称补丁可复现。

USB 默认拒绝打开；实际激活还需确认序列号、板型、正常固件、alt0 状态和
后续 P3/P4 profile/版本/safety 屏障。GET_INTERFACE 在锁定 Panda 固件中未实现，
不能把 alt0 描述符当作当前 alternate 的证明。传输库不会设置 alternate。

原 SPI 运行路径未改动。新适配器保持成功请求 wire 格式，但有意将无限重试
改成有限错误返回；不自动重发不确定写入/已消费读，也不转向 USB 或另一块板。
OS open/claim 和 spidev ioctl 的硬阻塞时间未得到保证。health/heartbeat
预算目前是纯策略测试；真正调度、整组失效和发送屏障属于 P3/P4。
P2 不接入 manager/pandad，不提供安装、刷写、复位或启动设备的入口。
