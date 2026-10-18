# P7 维护与离线复验

P7 是外层独立工程的维护封装。官方子模块与 P0–P6 补丁继续冻结；没有虚构新的 host/Panda 功能补丁，也没有新增 opendbc 车型或 safety 补丁。

- `downstream-lock.json`：官方四个提交、六个主机补丁、一个 Panda 补丁、依赖锁、验收定义、维护程序与 CI 的内容哈希。
- `core.py`：锁检查、干净补丁重放、确定性源码内容包。
- `validation.py`：新建一批 P5/P6 验收，第二组实际 ARM 编译，产物和发布证据核对。
- `runtime_guard.py`：只读检查外部提供的合成身份与本地候选文件，没有启动器、设备发现或自动修复动作。
- `release_policy.json`：41 项离线必选项和八类尚未通过的部署门槛。

从工程根目录执行。以下命令不联网、不连接 C3XL，缺少依赖直接失败。首次准备环境属于单独审查的准备工作；P7 不调用 bootstrap 或包管理器。

```sh
# 仅版本控制内的契约；不证明可以编译
python3 tools/songplus_compat.py verify-lock --metadata-only

# 已准备的官方源码、Git links、工具链和缓存依赖
upstream-openpilot/.venv/bin/python tools/songplus_compat.py verify-lock

# 每次使用不存在的新目录；生成 host/、panda/、opendbc/ 和 source.tar
upstream-openpilot/.venv/bin/python tools/songplus_compat.py replay \
  --output results/p7-replay-001

# 一次完整维护验收，包含上面两步、P5/P6、新的双路径构建和 release-check
upstream-openpilot/.venv/bin/python tools/check_panda_ci.py --mode full \
  --linux-instance byd-recovery-311 \
  --linux-root /home/shuang.guest/songplus-p2-build \
  --output results/p7-full-001

# 完整证据和本地产物必须仍在；离线通过且部署被阻止时返回 3
upstream-openpilot/.venv/bin/python tools/songplus_compat.py release-check \
  --report results/p7-full-001/validation/result.json
```

`validate` 可单独使用，参数是 `--linux-instance`、`--linux-root`、`--output`。Linux instance 指已经准备好的本地 Lima VM，不是车辆或设备地址。现有依赖锁包含明确的 macOS 绝对路径和 Linux 前缀；完整运行要求这些已声明的环境。它不承诺任意主机首次克隆后即可离线构建。源码契约 CI 只需 Python 标准库。

`source.tar` 固定顺序、mtime/uid/gid 和文件模式 0644，仅是源码内容包：host 覆盖实际 P6 声明的全部编译输入，Panda/opendbc 是完整源码。它不是完整 openpilot 分发包或可直接执行的 checkout；调用脚本时显式使用 Python 或 shell。包中不含 `.git`、缓存依赖、设备资料、系统镜像、安装器或 bootstub 产物。

每次完整运行要求干净重放、P5/P6 新报告、两处独立源码和输出目录中的八次 ARM 编译。相同缓存工具链下四个配置的 `.bin` 与开发签名 `.bin.signed` 必须逐字节相同；ELF/map 等逐项记录，不能把整个构建目录称为完全一致。见 [可复现范围](REPRODUCIBILITY.md)。正常 release-check 重验证报告链和 12 个保留的 ELF/bin/signed 文件；仅有版本化 JSON 摘要不能冒充本地完整候选包。

退出码：0 是请求的离线检查成功；2 是输入、锁、依赖、产物或验收失败；3 只用于 release-check，表示离线证据完整且部署仍被阻止。必须同时检查 JSON，不能把任意非零码都当作预期拒绝。

未知板型、协议、签名，或缺少产物，不能触发下载、编译、安装、DFU、刷写、重置、USB 角色切换或 bootstub 替换。即使合成身份和文件全部匹配，`runtime_available`、实物访问与 CAN TX 权限始终为 false。版本化报告区分主机测试、ARM 编译和 MCU/目标机/车辆执行，后者本阶段全部未执行。

更新流程：先审查新官方提交、子模块与依赖差异；在独立分支修改并导出相应补丁；更新该阶段的 manifest/映射；再审查 P7 lock 的 series 和 files 哈希变化，执行全新完整验收并保留旧报告。锁不会由常规验收自动改写。完整 validate 要求维护锁及其保护文件已提交且与 HEAD 一致，并记录该提交；其他用户文档的未提交修改不会被纳入或覆盖。不能只改哈希或跳过测试来消除失败。P7 的维护文件与原补丁一起通过外层 Git 提交保存。

[CI 的触发条件及环境配置](CI.md) 和 [发布门槛](release_policy.json) 是后续维护入口。GitHub 配置尚未推送或远端执行；本地执行共享 CI 命令不等于云端 Actions 成功。

本阶段的版本化验收摘要保存在 `validation/panda-compat-p7/`，本机完整运行位置由 `results/panda-compat-p7/latest.json` 指向。摘要保留证据 JSON、日志和文件哈希，ARM 二进制留在忽略的本地结果目录；因此发布复核必须指向完整本地结果，不能把摘要目录当作可发布包。旧 P5/P6 文件的“remaining P7”字段是历史阶段报告原文，不改写；当前阶段结论看 P7 总报告。
