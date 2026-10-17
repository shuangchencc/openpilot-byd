# P3 独立补丁与离线复验

在固定官方 openpilot `35e78a9d9518855ec0dacec919e719624a28131f` 及原
P0/P1/P2 三份补丁之后追加 P3。原补丁、依赖锁和历史结果不重写，官方
子模块不变。开发分支为 `songplus/dos-usb-p3`；顺序、提交、补丁和最终
文件哈希全部记录于 `manifest.json`。

P3 提供 C++ 固定槽位会话注册表、连接 epoch/整组代次，以及独立 CAN
路由/队列层。类型化 profile 逐项保持 P0 的合成身份、能力、角色与权限
约束；Python 独立对照使用原 P0 验证器和注册表，不引入第二个生产 JSON
读取器。所有 profile 仍只接受 fixture 身份和 noOutput，真实设备/TX/
固件权限恒为 false。路由输出是模拟发送意图，不是实际发送许可。

## 复验命令

```sh
upstream-openpilot/.venv/bin/python tools/validate_panda_sessions.py \
  --linux-instance byd-recovery-311 \
  --linux-root /home/shuang.guest/songplus-p2-build
```

指定的是已准备好的本机 Lima Linux 构建 VM，不是 C3XL。复用 P2 任务目录
已解包的 libusb；依赖锁仍为 `../p2/host-dependencies.json`。验证器不会
启动/安装 VM、联网下载、安装依赖或访问 Panda。缺依赖、哈希漂移、缺少
测试证据会失败。Linux 根目录参数指向构建缓存，不包含设备连接配置。

验证先把官方源码解包到新临时目录，检查四份补丁和前序 manifest 的依赖
哈希，按序应用，再核对所有结果文件。随后在网络/设备节点隔离环境中
执行 P0/P1/P2 回归，以及 P3 的 macOS/Linux 编译与测试；两平台的 native
oracle 输出都交给固定 Python3.12.13 的独立 P0/CAN 对照检查。

源码验收仍检查原 `spi.cc`、`panda_comms.h`、`panda.cc`、`pandad.cc`、
`SConscript` 和包初始化文件没有改变。Linux 链接真实 USB/SPI 后端，
实际读写仅由集成测试中的 fake P2 Backend 执行。没有完整活动 pandad
集成、MCU 构建、AGNOS 修改或实机验收。

详细日志在忽略的 `results/panda-compat-p3/`，版本化结果在
`validation/panda-compat-p3/`。原冻结设计中的 NOT_RUN 是历史快照，
各阶段实际执行状态以对应报告为准。

## 继续维护

从固定 P2 提交继续 P3 下游分支；只提交兼容层变更，导出新增补丁，
更新 manifest 的提交/哈希/验收映射后运行干净重放。不要更新官方 gitlink
或重写旧 P0/P1/P2 补丁来掩盖问题。P3 的候选资源与时间界限保存在新增
`compatibility/P3-BUDGET.md`，与 P2 内部传输等待限制分层记录。

后续 P4 才接入活动调度、消息有效性消费者、health/heartbeat 和逐板 safety
读回屏障。P3 的完整组可用性只证明身份/协议/连接结构完整，不能表示已经
允许车辆控制；掉板后也不能自动恢复接合状态。当前没有设备安装入口。
