# 当前启动与退出链：只读源码分析

源码采样时间：2026-10-20 01:17:06（Asia/Shanghai），即 2026-10-19 17:17:06 UTC。
独立加载态/标记补证时间：2026-10-20 01:32:55（Asia/Shanghai），即 2026-10-19 17:32:55 UTC。
本报告只分析本轮现机读取的固定启动/退出源码和 `systemctl show` 输出；没有执行、导入或解密目标源码。
profile 保持 **UNBOUND**，未进行服务操作、系统/槽位修改、Panda 主动访问、重置、刷写或 CAN 发送。

结论是：**现机加载的停止入口已核实，但它不能证明 manager/Panda 会完成有序退出。**
当前服务通过 tmux 会话启动和停止程序；manager 与 Panda wrapper 是 PyArmor 封装，内部的信号处理、
清理顺序和退出时板卡操作仍不透明。重启入口含系统初始化、条件更新和依赖安装，不能作为无副作用的返回手段。

## 证据及私有材料

工程外私有档案位于
`/Users/shuang/Code/openpilot/migration-audit/current-host-recovery-20261019T171237Z/startup/`。
`receipt.json` 将设备源码路径映射到 `source-NN.bin`；已对 14 个文件逐一重新核对长度和 SHA256。
14 个文件合计 172,795 字节，其中 12 个可作明文静态分析、2 个是 PyArmor envelope。
另 5 个固定源码候选和 3 个动态路径返回 `FileNotFoundError`；这是采样时的缺失观察，不是永久不存在的保证。

完整源码、查询原文与 base64 不进入工程。对象摘要、来源、行号与分析限制见
[startup-exit-analysis.json](startup-exit-analysis.json)。
第二轮的私有查询保存在同一档案集下的 `loaded-exit/`，没有改写第一轮源码或 receipt。

| 私有证据 | 长度 | SHA256 |
|---|---:|---|
| receipt.json | 23,023 | `790a7896305ed0443cd5245439ee4ec9f16e90b6678664817799cbad1b803aa0` |
| startup.json | 247,618 | `6406f104ee40d344427da6799144d9772b35ac89a42e312c8fd4dfe4aba1a50e` |
| systemctl stdout，采样前/后相同 | 770 | `7b335cafc6bf89fc4db435f5c194e45003810fc8f6f3fc87b2d0d6861f0eb325` |
| loaded-exit/receipt.json | 14,381 | `eacf59f57d50c9fdb86a21e585fef294eb11ed4a65e8b733092ceaf629893099` |
| loaded-exit/loaded-exit.json | 11,628 | `bc1fd290e4e2c5b85c0c2018d3f1b4c8f0a60e5f92f79e4c1e4fa4c2d0dcd2a9` |
| 第二轮 systemctl stdout，前/后相同 | 794 | `e7f5969898ac84d8575e3e511a091b47c4970daec3805f3322d5f159f71368b3` |

文件采集记录保留 mode、uid、gid；每个文件在固定 inode 上读取，读取前后大小/mtime/ctime 一致。
这些字段不是 ACL、扩展属性、SELinux 标签、硬链接关系或整个目录树的完整备份。
读取可能更新 atime，SSH/查询可能留下系统日志；没有主动写入目标内容。

## comma.service 的磁盘内容与已加载状态

当前 `/usr/lib/systemd/system/comma.service` 是 367 字节，SHA256
`eb5b82d979b5e94cdf2f089736a9d72e1b72cd08a465393405fb1c24057248bb`。
磁盘 unit 声明 `Type=oneshot`、`RemainAfterExit=true`、`User=comma`，停止超时为 1 秒。
它与旧 root 快照中的 `Restart=always` unit 不同；旧 unit 不适用于当前退出方案。

两轮各自的已加载查询前后完全相同，退出码为 0、stderr 为空。第二轮使用独立固定的 `show --all` 查询，
增加 Type/User，并再次请求三个未返回的 hook。两轮共同返回的字段逐项完全一致，确认：

| 已加载字段 | 本轮观察 |
|---|---|
| Type / User | 第二轮明确返回 oneshot / comma，与第一轮磁盘 unit 一致 |
| LoadState / ActiveState / SubState / Result | loaded / active / exited / success |
| FragmentPath | `/usr/lib/systemd/system/comma.service` |
| DropInPaths / NeedDaemonReload | 空 / no |
| UnitFileState / RemainAfterExit | enabled / yes |
| Restart / RestartUSec | no / 100ms；后者不构成启用自动重启的证据 |
| KillMode / KillSignal / FinalKillSignal | control-group / 15（SIGTERM）/ 9（SIGKILL） |
| SendSIGKILL / TimeoutStopUSec | yes / 1s |
| ControlGroup | `/system.slice/comma.service` |
| Triggers / TriggeredBy | 本次返回空 |

磁盘和已加载的命令均指向相同的静态入口：ExecStart 经 `/bin/bash -c` 创建名为 `comma` 的 tmux 会话，
由 `/usr/comma/comma.sh` 启动软件；ExecStop 经 `/bin/bash -c` 执行 `tmux kill-session -t comma`。
本轮没有执行这些命令。查询返回 ExecMainPID=3140，但 `active/exited` 下这个字段不能证明 PID 3140 仍存活，
也不是 USB/SPI 持有者的身份凭证。

第一轮没有返回 ExecStartPre、ExecStartPost、ExecStopPost；第二轮即使增加 `--all`，三个字段仍未返回。
不能把未返回误记为空或不存在；当前磁盘 unit 文本没有这三个赋值，仅证明磁盘文本层面的情况。
Type、User 在第一轮未查询，现已由第二轮加载态补齐。
`DropInPaths` 空、`NeedDaemonReload=no` 和主要命令一致，支持当前主要停止设置的解释，
仍不等于对全部加载属性、启动环境、外部重新拉起来源的完整证明。

## 当前启动链及返回时的副作用

磁盘调用链为：comma.service → tmux 会话 → comma.sh → continue.sh → launch_openpilot.sh →
按 DT model 分派的 C3 launcher，或根目录 fallback launcher → manager.py。
此前已观测 DT model 为 `comma tici`；本轮没有重走分支，磁盘链不能替代运行中进程的内存版本证明。

| 对象 | 已核实的静态行为及条件 |
|---|---|
| comma.sh（106 行） | source `/etc/profile`；等待 weston；调整 `/data` 与 `/data/media` 所有者；处理安装阶段 SSH/setup 参数；清理并重建 `/data/tmp`，创建编辑器目录链接；有 continue.sh 则 exec 它，否则进入安装器等待流程。 |
| comma.sh 的恢复分支（48–66 行） | 仅当 `/tmp/booted` 不是普通文件时进入本段，并创建该标记；之后检查 reset 标记、触摸次数或 userdata 未挂载条件，调用 reset/format/recover 分支。第二轮对 `/tmp/booted` 仅 lstat，返回 FileNotFoundError；不能依赖该标记已存在来跳过未来重启时的恢复检查。触摸计数及 reset 二进制未采，不判断未来实际会否 reset。 |
| continue.sh | 切到 `/data/openpilot`，exec `./launch_openpilot.sh`。 |
| launch_openpilot.sh | ERR trap 转入根 launcher；可 source `/data/custom_op_script.sh`；model 为 comma tici 时 exec C3 launcher。custom 脚本本轮 metadata 返回不存在，没有读取该脚本或运行 trap。 |
| 两份 launch_env.sh | 字节相同；在环境未预设 AGNOS_VERSION 时设为 12.4，并设 staging 目录。未读取当前环境，不能假设运行中的值必然等于默认值。 |
| C3 launcher | 还含目录链接及条件 SSH-agent 初始化；后者可能改写用户启动文件。只读取脚本文本，未读取/使用脚本引用的任何私钥。 |
| 两份 launcher 的 agnos_init | 含清理网络元数据、设置当前启动成功标记、修改设备节点权限；root launcher 在 VERSION 不等于目标时走更新检查，C3 launcher 在 VERSION 数值小于目标时走更新检查；更新路径可调用 reboot/updater。 |
| 两份 launcher 的 launch | 可移除 git lock、根据 staging 标记替换软件目录并 exec；改写 pythonpath 链接/日志；调用离线 wheel 检查修复，必要时写 `/data` 依赖目录或安装到解释器目录；无 prebuilt 时运行 build，再执行 manager。 |
| manager 返回后的 launcher | 进入 `while true` sleep 循环；这段脚本不会自行再次调用 manager。opaque manager 内部或其他监督者是否拉起进程另属未知。 |

这次读取的是当前有 wheel 修复代码的 launcher：root 文件 SHA256 为
`9ac1b4c94cdedd71465230744af592a0ad32ab59f52b745d92a80cd3810c2366`，C3 文件 SHA256 为
`db09a8ef86bac284f75a34145488a67eb3f01bd321691b4b8ed00a1ce30aa8f0`。
不能用早期较短的工厂 launcher 文本替代当前实现。

## 已知退出机制与仍不透明的部分

当前 `system/manager/process.py` 是明文，SHA256
`e1967e0e226285b91eb97b257b13eaff6ad842672591d90df3cb4ad139fc6ee7`：

- `ManagerProcess.stop()` 默认发 SIGINT（或 sigkill 标志要求的 SIGKILL），block 模式等待 5 秒；仍未退出且允许 retry 时再 SIGKILL。它向登记的单个 PID 发信号，不能据此推断其孙进程、板卡或总线均已清理。
- `restart()` 先显式 SIGKILL 再 start；watchdog 可触发 restart。不能把单独杀死一个 Panda 进程当作稳定隔离。
- `DaemonProcess.stop()` 为空实现；该类旨在跨 manager 重启存活，并用新进程组启动。新进程组不等于已逃离 systemd cgroup，不能据此断言它会跨整个 unit 停止存活。
- `ensure_running()` 对不应运行项调用非阻塞 stop，对应运行项调用 start。opaque manager 是否以及如何使用这些函数，未被本次静态读取证明。

`process_config.py` 明文将逻辑名 pandad 配置为 `selfdrive.pandad.pandad` 的 PythonProcess，条件为 always_run；
另一个 `_pandad` NativeProcess 条目明确 disabled。这里的 always_run 只是配置函数返回值，
不是整个服务自动重启策略，也不证明 wrapper 内部实现。

当前 manager.py（66,639 字节，SHA256 `d0c8e0c3f37850fb2a00d8a462f248c8f9802da12b556882bbde7f2e1d9f1147`）
及 pandad.py（36,676 字节，SHA256 `4ec2eec203b3b341b07deef604bf8598f852356c41212ddc5bd3b3e623601268`）
都是 PyArmor envelope。未导入、运行或解密。无法证明当前 manager 在 SIGTERM/HUP 下会调用哪些清理函数，
wrapper 是否在退出时写 Panda、关闭/重置哪个接口，或 native pandad 的最终 safety/heartbeat 状态。
预期路径上的 `pandad.cc` 本轮不存在，也没有取得当前 native binary 的退出实现。

当前 `system/hardware/tici/hardware.py` 静态定义了 reboot、poweroff，以及写 reset 标记后 reboot 的 uninstall 方法。
这只证明有这些能力；不能证明 opaque manager 在某次退出中调用它们，也不能据此认定它不会调用。

因此，**tmux ExecStop + systemd 的 1 秒停止设置，与 process.py 的 5 秒有序停止函数并不等价**。
1 秒不是被证明的全链路清理时间；不能拿服务 inactive、tmux 会话消失或 main PID 退出中的任一单项当作独占完成。
本报告没有执行停止实验，也没有证明板卡退出后的安全状态。

## 覆盖边界与后续验收条件

本轮补齐的是当前 14 个选定文件的字节材料、基本 mode/uid/gid 和关键已加载属性。
它不是完整 application tree、依赖 wheel/site-packages、软件模型、共享 params、用户数据或整机恢复集。
第一轮两个 query 相同只说明约 83ms 的窗口中这些属性没有观察到变化；第二轮窗口约 70ms，
也只证明当时的重复查询一致。共同字段相同不证明两轮之间从未发生变化。跨文件快照并非原子，
也未证明运行中的内存代码与所读磁盘字节一致。第二轮只取标记元数据，没有读取标记内容或创建标记。

在任何后续获授权的隔离实验前，仍需要：

1. 明确私有恢复对象的覆盖与一致性等级，保留未知/未采对象；当前材料不得标记为已经验证可恢复。
2. 若要证明没有 loaded pre/post hook，还需不同的只读加载态证据：本轮已尝试 `show --all`，三个字段仍未返回。必要启动依赖和 opaque manager/Panda 的退出逻辑继续保留缺口；不能改用旧明文实现推断。
3. 实验时重新核对当时进程/cgroup/句柄与监督关系；前次 PID、cgroup 或磁盘 hash 不能作为未来固定身份。
4. 有单独授权后才执行服务隔离，并同时验证 unit/tmux/进程与 USB/SPI 句柄全部退出、无重新拉起、无其他访问者；还需独立验证板卡状态，不能把主机退出等同于 Panda 已安全复位。
5. 返回路径单独验收。重新执行现有启动链会触发条件性写入，不能承诺“启动原服务就无副作用地恢复原态”。

本轮没有扩大上述操作权限。动态路径采样缺失、有限字段查询及明文辅助模块分析不能解除 UNBOUND 或 P10 的生产门槛。
