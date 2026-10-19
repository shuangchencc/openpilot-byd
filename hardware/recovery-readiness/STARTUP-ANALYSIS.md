# 当前启动监督链：本地来源复核与隔离准备

**已找到 5 个当前启动文件的同 SHA256 本地内容；当前 comma.service、comma.sh 和 rc-local.service 的完整内容仍未知。不能据此执行停服务或重新启动。**

本报告绑定北京时间 **2026-10-20 00:49:00** 的
[supervisor.json](/Users/shuang/Code/openpilot/songplus-port-lab/hardware/recovery-readiness/evidence/20261020/supervisor.json)。
源文件、包成员、摘要和逐行证据列于
[startup-provenance.json](/Users/shuang/Code/openpilot/songplus-port-lab/hardware/recovery-readiness/evidence/20261020/startup-provenance.json)。
本地复核没有 SSH、目标代码导入、脚本执行或服务控制；没有读取密钥内容、私有配置备份或系统分区镜像。

## 源码身份先于内容推断

先以固定启动文件名搜索已有 migration-audit/op_rootdir，再对 36 个小文件逐个计算摘要。
额外仅静态解码已有两个应用 delta 安装包内的指定 launcher 成员；未执行包装脚本，也没有释放归档到设备或覆盖本地原文件。
只有字节数和 SHA256 都与当前样本相同的内容进入行为分析。搜索不是全盘内容检索；“未找到”限于此次范围。

| 当前文件 | 当前字节数 / SHA256 前缀 | 匹配结果与边界 |
| --- | --- | --- |
| `/data/continue.sh` | 63 / `d1e8752fd5b067f7` | 与已有 op_rootdir 对应件完全一致。 |
| `/data/openpilot/launch_openpilot.sh` | 578 / `3b77285e884f2393` | 与 SP2027 factory-evidence 小文件一致。 |
| `/data/openpilot/launch_env.sh` | 271 / `0387e4991ade0cf3` | 与 SP2027 factory-evidence 等副本一致；仅证明根目录这份 env。 |
| `/data/openpilot/launch_chffrplus.sh` | 10,476 / `9ac1b4c94cdedd71` | common/fixed delta 指定成员均一致，且匹配历史 installed-file-manifest。factory 的 2,748 字节版本不一致。 |
| `/data/openpilot/sunnypilot/system/hardware/c3/launch_chffrplus.sh` | 10,716 / `db09a8ef86bac284` | common/fixed delta 指定成员均一致，且匹配 installed-file-manifest。 |
| `/usr/lib/systemd/system/comma.service` | 367 / `eb5b82d979b5e94c` | 没有找到同摘要本地内容。op_rootdir 的 244 字节版本不同，不能沿用其命令或停止行为。 |
| `/usr/comma/comma.sh` | 2,585 / `932f6700e78900ba` | 没有找到同摘要本地内容。op_rootdir 的 2,294 字节版本不同，不能沿用其初始化/重置逻辑。 |
| `/usr/lib/systemd/system/rc-local.service` | 731 / `76b9c90a0a7b25cd` | 没有找到同摘要本地内容；当前 `/etc/rc.local` 样本返回不存在。不能据此排除其他启动机制。 |

当前 comma.service 的安全字段投影确认 `Type=oneshot`、`RemainAfterExit=true`、
`TimeoutStopSec=1`、`User=comma`，存在 ExecStart 和 ExecStop 行，已知解释器为 `/bin/bash`。
投影还观察到 comma.sh/tmux 字面路径，但**完整命令被哈希脱敏**；不能补猜 ExecStop 等于某条 tmux kill 命令。
磁盘 unit、drop-in、生成单元与 systemd 已加载配置是否一致也未核实。

## 当前进程关系与仍未知的 manager

本次非原子快照中，两名可见硬件持有者及向上直到 tmux server 的可见祖先均在
`/system.slice/comma.service`；最上层 systemd PID1 属于 `/init.scope`：

```text
systemd 1
└─ tmux: server 3197
   └─ bash 3207
      └─ python3 12799
         └─ python3 15747
            └─ selfdrive.panda 22855  [SPI FD35/36, rdev153:0]
               └─ pandad 46699       [USB FD13, rdev189:5]
```

每个节点同时记录 PID、PPID、starttime，祖先重读期间未见所校验身份改变。这仍不是同一时刻的全局进程快照。
没有读取 cmdline/environ，也没有把两个 `python3` 进程标成已确认 manager。
源码中存在 manager.py 调用，与当前进程身份确认是两件事。

扫描见 558 个 PID，存在 532 次 FD 目录权限拒绝、29 次 FD stat 权限拒绝及 1 次 FD 消失；未达到扫描数量/时间上限。
所以只能确认这些可见持有者，不能确认它们是全部访问者。
同一 cgroup 也不证明停一个 unit 后所有 master 都已退出，或外部 tmux server、别名、其他命名空间不存在。

## 同摘要启动脚本的已知副作用

以下均是**当前磁盘文件对应源码中的条件行为**，不是本轮执行记录，不证明这些分支在当前进程启动时已经发生。

1. **continue → launcher 跳转已能静态核对。** continue.sh 第 3–4 行进入 `/data/openpilot` 并 exec launcher。
   launch_openpilot.sh 第 6 行设置失败回退；第 13–15 行在文件存在时 source `/data/custom_op_script.sh`；
   第 17–22 行按 `comma tici` 模型选择 C3 launcher，其余或失败时使用根 launcher。
   本轮没有查询 custom script 是否存在，更没有读取其内容。存在该条件入口，因此不能宣称启动行为仅由已分析文件决定。
2. **根 launcher 的 AGNOS 初始化会产生写入。** 第 9–17 行包含清理指定 NetworkManager 元数据、
   `abctl --set_success` 和 GPU 相关节点的组/权限设置。第 20–26 行在版本条件满足时进入验证、重启或 updater 路径。
   当前根 launch_env 第 9–10 行只是在外部变量为空时设定 12.4；本轮未读取进程环境，不能据此保证更新分支不可达。
3. **两个 launcher 都可能替换软件目录、安装依赖并启动应用。** 根版本第 186–202 行、C3 第 197–213 行
   在 overlay 条件成立时移动目录并重新 exec；根第 212 行、C3 第 223 行更新 Python 路径链接。
   ensure_py_pkgs 可选择解释器、导入目标编译模块、创建 `/data/py_offline_site`、执行离线 pip 安装，部分分支有 sudo fallback。
   根第 226–230 行、C3 第 237–241 行可能 build 并运行 manager.py；启动后还会写 tmux capture 日志。
4. **C3 版本另有身份代理与用户文件副作用。** 第 4–11 行建立用户目录链接，并在特定密钥路径存在时启动 ssh-agent、
   向 shell profile 追加代理变量并执行 ssh-add。这里只静态读取脚本里的路径字面量，未读取密钥文件。
   C3 第 17 行 source 同目录 launch_env；这份现机 sibling env 没有进入本次样本，不能拿已确认的根 env 冒充它。
   C3 的版本比较分支和根 launcher 的比较条件也不同。

因此，**重新执行普通启动链不是无副作用的恢复按钮**。同样，不能从启动源码反推出安全的停止顺序：
当前 comma.service 的完整 ExecStop、当前 comma.sh、manager/Panda 退出和复位路径仍未闭环。
旧 SP098 明文流程、未经核实的 PyArmor 包装内容或旧 system image 的行为不能替代这些当前证据。

## 对后续隔离台架方案的约束

- 保持本轮“仅准备、不启停”边界。已知磁盘启动链需要明确写入/更新控制，不能作为只读取证入口。
- 先补齐当前 comma.service/comma.sh 的精确、受控静态内容或已加载安全字段，核实 drop-in、完整 ExecStop、KillMode 与 tmux session 范围。
  任何未知内容继续标为 unknown，不使用历史命令拼出待执行脚本。
- 将已确认 cgroup/PPID 关系用于拟定隔离对象，另行证明 manager 身份和真实停止/退出语义；PID 必须在未来执行时重采并与 starttime 校验，不能固化本次数字。
- 真正获准隔离后，验收应覆盖原持有者退出、是否被重新拉起、组完整性及新 FD/锁观察；还需处理权限缺口和全部 master 的合作条件。
  本报告没有执行这些操作，也不授予独占访问、真实 profile 或生产权限。
- 未来重新启动需单独审查上述更新、overlay、依赖修复、custom script、身份代理及 Panda 初始化路径，
  不以“恢复原状”为由自动执行。

当前状态保持 **UNBOUND / 隔离与恢复执行未验收**。
