"""本机局域网 IP 检测（纯 Python，零外部依赖）"""

import socket
import subprocess


def get_local_ips():
    """获取所有有效的局域网 IPv4 地址，返回 [(网卡名, IP), ...]"""
    results = []
    try:
        output = subprocess.run(
            ["ipconfig"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        current_adapter = None
        for line in output.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line and not line.startswith(" "):
                adapter_part = line.split(":")[0].strip()
                skip_keywords = ["VirtualBox", "VMware", "Hyper-V", "WSL", "Bluetooth", "vEthernet"]
                if not any(kw in adapter_part for kw in skip_keywords):
                    current_adapter = adapter_part
            elif "IPv4" in line and current_adapter:
                parts = line.split(":")
                if len(parts) >= 2:
                    ip = parts[-1].strip()
                    if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                        results.append((current_adapter, ip))
    except Exception:
        pass

    if not results:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("114.114.114.114", 53))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                results.append(("默认", ip))
        except Exception:
            results.append(("回退", "127.0.0.1"))

    return results


def get_preferred_ip():
    """获取首选局域网 IP：优先 192.168.x.x，其次 10.x.x.x，再次 172.x.x.x"""
    ips = get_local_ips()
    if not ips:
        return "127.0.0.1"
    for _, ip in ips:
        if ip.startswith("192.168."):
            return ip
    for _, ip in ips:
        if ip.startswith("10."):
            return ip
    for _, ip in ips:
        if ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31:
            return ip
    return ips[0][1]
