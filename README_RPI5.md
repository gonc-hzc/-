# Raspberry Pi 5 一体化控制入口

`rpi5_control.py` 用于将现有遥控器整体迁移到 Raspberry Pi 5。它在一个进程中完成：

- 托管手机或电脑使用的遥控网页。
- 接收网页摇杆发送的 WebSocket 指令。
- 通过 UART、BLE UART 或 TCP 测试链路转发到 STM32。
- 页面失联、输出关闭或控制页面退出时重复发送停车帧。
- 只允许最后打开的控制页面接管输出。

树莓派入口使用独立的 `frontend-rpi5` 页面目录。原有电脑版本继续使用 `frontend`，两套入口互不覆盖。

## 1. 树莓派准备

建议使用 Raspberry Pi OS Bookworm，并为项目建立虚拟环境：

```bash
sudo apt update
sudo apt install -y python3-venv
cd ~/remote-control
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-rpi5.txt
```

UART 连接 STM32 时，在 `sudo raspi-config` 中启用串口硬件并关闭串口登录 shell。GPIO 14 为 TX，GPIO 15 为 RX，树莓派与 STM32 必须共地，电平必须为 3.3V。

## 2. 运行

UART 是默认模式：

```bash
.venv/bin/python rpi5_control.py --transport serial --serial /dev/serial0
```

BLE UART 模式：

```bash
.venv/bin/python rpi5_control.py --transport ble --address 19:D6:51:C4:64:57
```

本机联调模式：

```bash
python mock_stm32.py
python rpi5_control.py --transport tcp --tcp 127.0.0.1:9000
```

运行后，在同一局域网中的手机或电脑打开：

```text
http://<树莓派IP>:8000
```

页面打开后，需要手动开启页面上的输出开关。UART、BLE 和 TCP 模式下都会生效。关闭后会主动停车并断开下游链路。

## 3. 协议选择

默认使用：

- UART 和 TCP：完整 JSON，例如 `{"type":"rc","seq":1,"throttle":300,"turn":0,...}`。
- BLE：短 JSON，例如 `{"v":300,"w":0}`。

如 STM32 固件解析器不同，可显式传入：

```bash
python rpi5_control.py --packet-format json
python rpi5_control.py --packet-format minjson
python rpi5_control.py --packet-format compact
```

## 4. systemd 自启动

将项目放到 `/home/pi/remote-control` 后，新建 `/etc/systemd/system/rpi5-control.service`：

```ini
[Unit]
Description=Raspberry Pi 5 robot remote control
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/remote-control
ExecStart=/home/pi/remote-control/.venv/bin/python rpi5_control.py --transport serial --serial /dev/serial0
Restart=on-failure
RestartSec=2
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rpi5-control
sudo systemctl status rpi5-control
```
