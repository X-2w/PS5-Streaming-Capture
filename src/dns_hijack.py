"""DNS 劫持模块 — UDP 53 监听，匹配 Twitch ingest 域名返回本机 IP"""

import socket
import selectors
import threading
import traceback

from dnslib import DNSRecord, DNSHeader, RR, A, QTYPE, RCODE

from config import (
    DNS_PORT,
    DNS_UPSTREAM,
    DNS_UPSTREAM_BACKUP,
    DNS_TTL,
    HIJACK_DOMAINS,
    HIJACK_SUFFIXES,
    EXCLUDE_DOMAINS,
    EXCLUDE_SUFFIXES,
)


class DNSHijacker:
    def __init__(self, local_ip: str, log_callback=None):
        self.local_ip = local_ip
        self.log = log_callback or (lambda msg: None)
        self.running = False
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stats = {
            "matched": 0,
            "forwarded": 0,
            "forward_fail": 0,
            "parse_fail": 0,
            "total": 0,
        }

    def _should_hijack(self, qname: str) -> bool:
        """判断域名是否应被劫持"""
        if qname in EXCLUDE_DOMAINS:
            return False
        for suffix in EXCLUDE_SUFFIXES:
            if qname.endswith(suffix):
                return False
        if qname in HIJACK_DOMAINS:
            return True
        for suffix in HIJACK_SUFFIXES:
            if suffix.startswith("."):
                if qname.endswith(suffix) or qname == suffix[1:]:
                    return True
            else:
                if suffix in qname:
                    return True
        return False

    def _build_response(self, request: bytes, qname: str) -> bytes:
        """构造劫持响应，返回本机 IP"""
        dns_req = DNSRecord.parse(request)
        reply = DNSRecord(
            header=DNSHeader(
                id=dns_req.header.id,
                qr=1,
                aa=0,
                ra=1,
                rcode=RCODE.NOERROR,
            ),
            q=dns_req.q,
        )
        reply.add_answer(RR(rname=qname, rtype=QTYPE.A, rclass=1, ttl=DNS_TTL, rdata=A(self.local_ip)))
        return reply.pack()

    def _forward_to_upstream(self, request: bytes, upstream_ip: str, timeout: float) -> bytes | None:
        """向单个上游 DNS 转发请求并返回响应，失败返回 None"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as us:
                us.settimeout(timeout)
                us.sendto(request, (upstream_ip, 53))
                response, _ = us.recvfrom(4096)
                return response
        except Exception:
            return None

    def _forward_request(self, request: bytes, addr, qname: str = "", qtype_name: str = "") -> None:
        """透明转发 DNS 请求到上游，优先主 DNS，失败则尝试备用"""
        response = self._forward_to_upstream(request, DNS_UPSTREAM, timeout=1.5)
        if response is not None:
            try:
                self.sock.sendto(response, addr)
                self.stats["forwarded"] += 1
                return
            except OSError as e:
                self.stats["forward_fail"] += 1
                return

        response = self._forward_to_upstream(request, DNS_UPSTREAM_BACKUP, timeout=2)
        if response is not None:
            try:
                self.sock.sendto(response, addr)
                self.stats["forwarded"] += 1
                return
            except OSError as e:
                self.stats["forward_fail"] += 1
                return

        self.stats["forward_fail"] += 1
        self.log(f"[DNS] 上游 DNS 无响应: {qname}")

    def _run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)

        try:
            self.sock.bind(("0.0.0.0", DNS_PORT))
        except PermissionError:
            self.log("[DNS] 绑定失败：需要管理员权限")
            self.running = False
            return
        except OSError as e:
            self.log(f"[DNS] 53 端口被占用: {e}")
            self.log("[DNS] 请关闭其他 DNS 服务后重试")
            self.running = False
            return

        self.sock.setblocking(False)
        sel = selectors.DefaultSelector()
        sel.register(self.sock, selectors.EVENT_READ)

        self.log(f"[DNS] 服务已就绪，监听 0.0.0.0:{DNS_PORT}，开始接管局域网 DNS 请求")
        if DNS_UPSTREAM != DNS_UPSTREAM_BACKUP:
            self.log(f"[DNS] 上游 DNS: {DNS_UPSTREAM} / 备用: {DNS_UPSTREAM_BACKUP}")
        else:
            self.log(f"[DNS] 上游 DNS: {DNS_UPSTREAM}")

        while self.running:
            events = sel.select(timeout=0.5)
            for key, _ in events:
                try:
                    data, addr = key.fileobj.recvfrom(65535)
                    self.stats["total"] += 1
                    client_ip = addr[0]

                    try:
                        dns_req = DNSRecord.parse(data)
                    except Exception:
                        self.stats["parse_fail"] += 1
                        if self.stats["parse_fail"] <= 3:
                            self.log(f"[DNS] DNS 请求解析失败 ← {client_ip} | 累计 {self.stats['parse_fail']} 次")
                        continue

                    qname = str(dns_req.q.qname).rstrip(".")
                    qtype_name = QTYPE.get(dns_req.q.qtype) or f"TYPE{dns_req.q.qtype}"

                    if self._should_hijack(qname):
                        response = self._build_response(data, qname)
                        key.fileobj.sendto(response, addr)
                        self.stats["matched"] += 1
                        self.log(f"[DNS] 劫持 {qname} → {self.local_ip}")
                    else:
                        self._forward_request(data, addr, qname, qtype_name)

                except OSError as e:
                    self.log(f"[DNS] 网络异常: {e}")
                except Exception:
                    self.stats["parse_fail"] += 1
                    if self.stats["parse_fail"] <= 3:
                        self.log(f"[DNS] 未知错误:\n{traceback.format_exc(limit=3)}")

        sel.unregister(self.sock)
        self.sock.close()
        self.sock = None
        self.log(f"[DNS] 已停止 | 共处理 {self.stats['total']} 次查询，拦截 {self.stats['matched']} 次")

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        self.thread = None
