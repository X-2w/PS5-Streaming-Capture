---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 298632bafbb2d7f7ee80ab68387f191b_bf0d636e7c6911f18d11525400e6dd8f
    ReservedCode1: HkuShwP319wlyospJV8ysOvbj2BEzZ0bf9Asyk4KfDPxz78Mj/cZWftq+rD9h4ITfh5U3FDQQ9d8NtcDUpD/cJTASKqeZyKvWoKrJvlSUH0I0NJkRaseaTyTb6K18yv/aim9kyrpzYovc56+E3BW5W+4Wdjw7QDcnc6oRdR7eFjLajbDiEx3qWgyOuw=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 298632bafbb2d7f7ee80ab68387f191b_bf0d636e7c6911f18d11525400e6dd8f
    ReservedCode2: HkuShwP319wlyospJV8ysOvbj2BEzZ0bf9Asyk4KfDPxz78Mj/cZWftq+rD9h4ITfh5U3FDQQ9d8NtcDUpD/cJTASKqeZyKvWoKrJvlSUH0I0NJkRaseaTyTb6K18yv/aim9kyrpzYovc56+E3BW5W+4Wdjw7QDcnc6oRdR7eFjLajbDiEx3qWgyOuw=
---

# PS5 直播截获工具

> 无需采集卡，通过 DNS 劫持将 PS5 的 Twitch 直播流截获到本机，OBS / VLC 直接拉流。

## PS5 配置教程

1. PS5 主界面进入 **设置 → 网络 → 设定 → 设定互联网连接**
2. 选中当前网络 → 按手柄 **Options 键** → **高级设定**
3. DNS 设置选择 **手动**
4. 首选 DNS 填入工具界面显示的 **本机 IP 地址**
5. 保存后进入游戏，按 Share → **开始直播**

## 功能特性

- **DNS 劫持**：拦截 PS5 对 Twitch ingest 域名的 DNS 查询，将流量导向本机
- **RTMP 接收**：内置 nginx-rtmp 服务，接收 PS5 推流并发布为本地 RTMP 地址
- **三栏实时面板**：左栏服务状态 → 中栏实时带宽(MB/s) + DNS 统计 → 右栏 60 秒折线图
- **一键启停**：启动 / 停止全部服务，自动端口冲突检测与警告
- **单文件 exe**：PyInstaller 打包，分发即用

## 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| GUI | tkinter（标准库，零额外依赖） |
| DNS 劫持 | dnslib（UDP 53，伪造 A 记录 + 透明转发） |
| RTMP 服务 | nginx + nginx-rtmp-module（Windows 预编译版） |
| 打包 | PyInstaller |
| 目标平台 | Windows 10/11 |

## 架构

```
┌──────┐   DNS Query (UDP 53)   ┌──────────┐   透明转发    ┌────────────┐
│ PS5  │ ──────────────────────▶│ DNS 劫持  │─────────────▶│ 上游 DNS    │
│      │                        │ (匹配拦截) │              │ 114.114...  │
│      │                        └──────────┘              └────────────┘
│      │   RTMP Push (TCP 1935)
│      │ ──────────────────────▶┌──────────────┐
│      │                        │ nginx-rtmp   │◀─── rtmp://127.0.0.1:1935/app/<key>
│      │                        │ :1935        │     (OBS / VLC 拉流)
└──────┘                        └──────┬───────┘
                                       │ HTTP :8080/stat (带宽统计)
                                ┌──────┴───────┐
                                │  GUI 面板     │
                                │  tkinter      │
                                └──────────────┘
```

**数据流**：
1. PS5 解析 Twitch ingest 域名 → DNS 查询被劫持 → 返回本机 IP
2. PS5 向本机 1935 端口发起 RTMP 推流
3. nginx-rtmp 接收推流，发布为 `rtmp://127.0.0.1:1935/app/<stream_key>`
4. OBS 添加媒体源，填入拉流地址即可

## 使用方式

### 开发调试

```powershell
# 安装依赖
pip install dnslib

# 以管理员身份运行（绑定 UDP 53 和 TCP 1935 需要）
python src/main.py
```

### OBS 拉流

1. 添加 **媒体源**
2. 取消勾选"本地文件"
3. 输入工具界面显示的拉流地址（格式：`rtmp://127.0.0.1:1935/app/<stream_key>`）

### 打包分发

```powershell
python src/build.py
```

## 运行前提

| 条件 | 说明 |
|------|------|
| 管理员权限 | 绑定 UDP 53 端口必须 |
| PS5 与本机在同一局域网 | DNS 劫持才能生效 |
| Windows 防火墙放行 | UDP 53、TCP 1935、TCP 8080 入站 |
| Python 3.10+ | 开发阶段需要 |

## 端口说明

| 端口 | 协议 | 用途 |
|------|------|------|
| 53 | UDP | DNS 劫持监听 |
| 1935 | TCP | RTMP 推流接收 |
| 8080 | TCP | nginx RTMP 统计页 (HTTP) |
| 8081 | TCP | 推流状态回调 (HTTP) |

## 目录结构

```
streaming/
├── src/
│   ├── main.py              # 主入口 + GUI 面板 (tkinter)
│   ├── config.py            # 全局配置（DNS / RTMP / nginx 路径）
│   ├── dns_hijack.py        # DNS 劫持模块（白名单匹配 + 透明转发）
│   ├── ip_detect.py         # 本机 IP 检测
│   ├── rtmp_manager.py      # RTMP 服务管理（nginx 启停 + 状态回调）
│   ├── build.py             # PyInstaller 打包脚本
│   └── requirements.txt     # Python 依赖
├── rtmp_server/
│   └── nginx/
│       ├── nginx.exe        # nginx-rtmp Windows 预编译版
│       └── conf/
│           ├── nginx.conf   # RTMP 服务配置（端口 1935 + 回调 8081）
│           └── mime.types
└── README.md
```

## 依赖

```
dnslib>=0.9.25
```

Python 标准库：`tkinter`, `socket`, `ssl`, `threading`, `subprocess`, `queue`, `xml.etree`, `urllib`

## License

MIT
*（内容由AI生成，仅供参考）*
*（内容由AI生成，仅供参考）*
