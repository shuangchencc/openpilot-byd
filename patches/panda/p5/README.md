# P5：DOS/F4 应用固件独立补丁

本目录只叠加到锁定的 Panda `a59195163c32a3a375ea6c86715dc08fd0984c1b`；
P0–P4 host 补丁仍从 `patches/openpilot/p4/manifest.json` 独立重放，未修改官方 gitlink。
完整来源、许可和参考引脚见补丁内 `board/stm32f4/SOURCE-MANIFEST.json` 与
`REFERENCE-BSP.md`。历史官方 F4 源码只恢复 BSP；协议与 safety 使用当前锁定源。

运行正式验收（已缓存依赖；不会安装或下载）：

```sh
upstream-openpilot/.venv/bin/python tools/validate_panda_firmware.py
```

入口校验锁定依赖、在新目录重放两个独立补丁栈，并用 macOS Seatbelt 禁止网络、
设备节点和 IOKit 打开。结果保存在 `results/panda-firmware-p5/`，版本化摘要见
`validation/panda-firmware-p5/`。正式验收不依赖工作分支未提交内容。

候选构建入口在补丁内 `tools/p5/build_candidate.py`，参数为 `--source`、`--opendbc`、
`--arm-prefix`（工具所在目录）、`--output`（空目录）、`--target f4|h7`、
`--policy debug|release`。只编译当前 `board/main.c` 和真正的 MCU startup，输出
ELF、map、bin、栈/调用图、编译日志、源码和产物哈希。两个 policy 都只使用仓库公开
开发密钥验证应用格式；`release` 指没有 `ALLOW_DEBUG`，不代表发布签名或可部署。
没有 bootstub、installer、复位、刷写或自动恢复目标。

F4 采用明确的 F413 DOS **公开参考配置**：1 MiB Flash 上限；仅分配低128 KiB RAM，
其中静态区122872 B、栈8192 B、cookies8 B；其他192 KiB不分配。RX256、每路TX64，
队列元素仍为官方72 B CANPacket，F4只允许classic CAN。IR、SPI、音频和电流检测
均不伪装成有效能力。新增0xd5扩展不修改原health/0xdd ABI；离线能力解码会将缺失
字段标为不可用。P4真实运行路径尚未绑定该扩展，实际profile继续未绑定。

签名校验通过仅证明公开应用格式；实物MCU、接线、振荡器、实际bootstub信任及恢复
链未确认。全部候选 `deployable=false`，不得把离线通过解释为可接车或可刷写。
P6的宋PLUS safety适配、完整双板故障闭环与后续P7交付仍未完成。
