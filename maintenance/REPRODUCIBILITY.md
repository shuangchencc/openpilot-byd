# P7 固件复现检查

`maintenance/reproducibility.py` 只读取两轮构建的文件，不启动编译器、网络、
设备或刷写工具。实际编译必须由 P7 总入口在两套干净源码与输出目录中执行，
并保留八次编译及资源审计的命令和日志。复制第一次输出不能作为第二次构建。

每轮根目录必须包含 `f4-debug`、`f4-release`、`h7-debug`、`h7-release` 四个目录。
每个目录保留 P5 构建器的原始 `build.json` 及其声明的十个产物：
`build.log`、`main.bin`、`main.bin.signed`、`main.ci`、`main.d`、`main.elf`、
`main.map`、`main.o`、`main.su`、`startup.o`。资源审计生成的 `resources.json`
由总入口另外检查；该比较器不能替代 RAM、栈、DMA 或引导区域审计。

调用方式（根目录需使用本轮实际路径）：

```sh
python3 maintenance/reproducibility.py \
  --first /absolute/first-run \
  --second /absolute/second-run \
  --public-key /absolute/first-run/panda/board/crypto/certs/debug.pub \
  --output /absolute/reproducibility.json
```

Python 接口是 `compare_build_sets(first: Path, second: Path, public_key: Path)`。
失败抛出 `ValueError` 或文件/解析异常，CLI 非零退出且不产生成功报告。
成功报告的 schema 为 `songplus-p7-firmware-reproducibility/v1`。

## 强制相等与单独报告的内容

| 内容 | 门槛 |
|---|---|
| 四种组合的 raw 与 development-signed 应用镜像 | 全部字节严格相等；不删除、替换或归一化任何字节 |
| 构建输入 | board/safety 内容摘要重算后匹配，协议头摘要对应同一输入，编译器标识相同 |
| 固件版本 | 从输入摘要、MCU、policy 推导；与 JSON 和 raw 中唯一的版本串一致 |
| ELF | 每轮确认 ARM ELF32、little-endian、ET_EXEC、EABI5 hard-float、正确 Cortex 与向量地址，所有加载字节对应 raw |
| ELF 运行布局 | 两轮入口、PT_LOAD 内存布局和全部 allocated sections 相同；不允许用调试路径差异掩盖内存布局变化 |
| 签名 | 每轮用已锁定公开 development 公钥验算官方 RSA1024/SHA1、长度和 VERS2 格式；release-policy 也不是正式发布签名 |
| 产物清单 | 全部声明哈希重算；拒绝漏项、空文件、路径穿越、符号链接、两轮硬链接别名 |
| 构建目录 | 拒绝同一/嵌套根目录；命令记录必须指向对应输出，源码、opendbc 和输出路径必须各自不同 |
| ELF、map、对象、依赖、栈记录与日志的整个文件 | 报告各自 SHA-256、长度与字节相等事实，不将其列为固件镜像复现的必要条件 |

P5 构建器使用 board/safety 内容摘要产生版本串，对 Panda、opendbc 和输出目录
设置 `-ffile-prefix-map`。签名脚本使用确定性的 development RSA 签名，不使用
时间或随机数。因此可以要求镜像原始字节一致。map、依赖、调用图、栈文件及
日志仍可能含绝对路径；`build.json` 的命令也有本轮路径。即使 ELF 实际相同，
也不据此宣称整个构建目录或不同编译器/操作系统之间已经实现字节复现。

该检查器不能仅凭元数据证明编译器真的执行过；P7 总入口负责独立重放源码、
检查工具链锁、创建空输出、实际执行命令并保存当次证据。结果不表示 MCU 已运行、
板卡识别已确认、安装/恢复兼容或车辆控制已验证，`deployable` 始终为 `false`。

## 小型离线负向夹具

```sh
python3 -m unittest maintenance.test_reproducibility -v
```

这些测试构造最小 ELF 与虚拟固件，覆盖字节漂移、哈希失配、截断、错误 MCU、
错误 ELF 类型/CPU、加载内容或内存布局变化、版本不符、复制元数据、复用源码、
漏项和文件别名等拒绝路径。签名夹具用恒等运算替代 RSA，以单独检验签名信封和
摘要；不接触私钥，不计作 ARM 编译或实际签名验证。正式八组构建的公开密钥验算
使用未经替换的真实算法与锁定公钥。
