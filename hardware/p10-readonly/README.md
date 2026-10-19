# P10 被动实机核实

2026-10-19结果见`RESULT-20261019.md`；可追溯数据在`evidence/20261019/index.json`。
本目录是独立硬件证据层，不改P0–P10维护锁、协议、固件、profile或部署权限。

`tools/inspect_c3xl_p10_readonly.py`默认只预览collector和SHA；只有明确的`--connect`
才通过现有SSH身份与已有host key运行系统Python stdin脚本。连接配置在忽略的
`local/c3xl-connection.json`，不保存认证内容。该入口不是CI、部署或原生Panda探测入口。

支持io、software、recovery、recovery-detail和filesystem五个阶段。不得随意增加目标路径、
控制请求或sudo；权限不足保留unknown。恢复阶段使用既有Step1有界GPT解析器，filesystem
只读两个DSP ext4 superblock。所有设备侧采样不安装文件、不执行目标openpilot代码，
不启停服务、不发USB/SPI控制请求、不发送CAN、不切槽、不reset/flash。

本地离线检查：

```sh
upstream-openpilot/.venv/bin/python -B -m unittest discover -s hardware/p10-readonly -p 'test_*.py' -v
upstream-openpilot/.venv/bin/python -B -m unittest discover -s hardware/step1/followup/tests -p 'test_collector.py' -v
```

样本是非原子观测。proc权限、命名空间、别名和并发进程会留下可见性缺口；无FD或无锁记录
不证明空闲。设备树、USB缓存描述符、磁盘文件摘要不等于物理身份或运行固件证明。
未知实际身份始终保持UNBOUND。
