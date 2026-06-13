"""Pikmin in-process WiFi tunnel infrastructure.

Provides:
- TunnelRunner: owns a single tunnel via RemotePairing + TCP tunnel
- TunnelRegistry: manages tunnels with watchdog + keepalive
- Discovery helpers: scan for RemotePairing devices

Runs on Python 3.13 (native TLS-PSK). The tunnel context stays open in a
background asyncio event loop so the RSD link remains usable.
"""

import asyncio
import logging
import threading
import time

logger = logging.getLogger("pikmin.wifi_tunnel")


# ═══════════════════════════════════════════════════════════════
# TunnelRunner — single in-process tunnel
# ═══════════════════════════════════════════════════════════════

class TunnelRunner:
    """Owns a single in-process WiFi tunnel via RemotePairing + TCP tunnel.

    The tunnel context (service.start_tcp_tunnel()) must stay open for the
    RSD link to remain usable. This class holds it inside a long-running
    asyncio task and releases it via a stop event.

    Tunnel death is detected via tunnel.client.wait_closed() — when the
    underlying TCP socket dies (OSError / ConnectionReset in sock_read_task),
    wait_closed() resolves and the runner exits so the watchdog can restart.
    """

    def __init__(self) -> None:
        self.info: dict | None = None           # {rsd_address, rsd_port, interface, protocol}
        self.task: asyncio.Task | None = None    # long-running _run() task
        self._stop: asyncio.Event = asyncio.Event()
        self._ready: asyncio.Event = asyncio.Event()
        self._error: BaseException | None = None
        self.target_ip: str | None = None
        self.target_port: int | None = None
        self.udid: str | None = None

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def _run(self, udid: str, ip: str, port: int) -> None:
        """Main tunnel loop. Blocks until stopped or TCP socket dies.

        Never re-raises; sets _error + _ready.set() so callers can
        retrieve the exception via start() without unhandled-task spam.
        """
        from pymobiledevice3.remote.tunnel_service import (
            RemotePairingTunnelService,
        )
        try:
            logger.info("Connecting RemotePairing %s:%d for %s", ip, port, udid)
            service = RemotePairingTunnelService(udid, ip, port)
            await service.connect(autopair=False)
            logger.info("RemotePairing connected (identifier=%s)", service.remote_identifier)

            async with service.start_tcp_tunnel() as tunnel:
                self.info = {
                    "rsd_address": tunnel.address,
                    "rsd_port": tunnel.port,
                    "interface": tunnel.interface,
                    "protocol": str(tunnel.protocol),
                }
                logger.info("Tunnel established: %s:%d", tunnel.address, tunnel.port)
                self._ready.set()

                # Race: stop signal vs tunnel death (wait_closed)
                stop_task = asyncio.create_task(self._stop.wait())
                closed_task = asyncio.create_task(tunnel.client.wait_closed())
                try:
                    await asyncio.wait(
                        [stop_task, closed_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in (stop_task, closed_task):
                        if not t.done():
                            t.cancel()
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass

                if self._stop.is_set():
                    logger.info("Tunnel stopped by request")
                else:
                    logger.warning("Tunnel TCP socket died — watchdog will restart")
        except BaseException as exc:
            logger.exception("Tunnel._run failed for %s @ %s:%d — %s: %s",
                udid, ip, port, type(exc).__name__, exc)
            self._error = exc
            self._ready.set()
            raise  # re-raise so the task ends with the exception (match locwarp)
        finally:
            self.info = None

    async def start(self, udid: str, ip: str, port: int, timeout: float = 20.0) -> dict:
        """Start tunnel and wait for RSD info.

        Returns dict with keys: rsd_address, rsd_port, interface, protocol.
        Raises asyncio.TimeoutError on timeout or the underlying exception.
        """
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._error = None
        self.info = None
        self.target_ip = ip
        self.target_port = port
        self.udid = udid
        self.task = asyncio.create_task(self._run(udid, ip, port))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._stop.set()
            try:
                await asyncio.wait_for(self.task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            self.task = None
            raise
        if self._error is not None:
            exc = self._error
            self.task = None
            raise exc
        return dict(self.info or {})

    async def stop(self) -> None:
        """Signal stop and wait for tunnel task to exit."""
        if not self.is_running():
            self.task = None
            self.info = None
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self.task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Tunnel task stuck; cancelling")
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass
        self.task = None
        self.info = None


# ═══════════════════════════════════════════════════════════════
# TunnelRegistry — tunnel lifecycle + watchdog + keepalive
# ═══════════════════════════════════════════════════════════════

class TunnelRegistry:
    """Manages in-process WiFi tunnels with watchdog + keepalive.

    Runs a background asyncio event loop in a daemon thread. All tunnel
    operations are executed on this loop. Provides blocking sync wrappers
    for use from tkinter callbacks.

    Usage:
        registry = TunnelRegistry(log_callback=print)
        registry.start()
        info = registry.start_tunnel_sync(udid, ip, port)
        registry.start_watchdog_sync(udid)
        registry.start_keepalive_sync()
        ...
        registry.stop()
    """

    def __init__(self, log_callback=None):
        self._tunnels: dict[str, TunnelRunner] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._watchdog_tasks: dict[str, asyncio.Task] = {}
        self._keepalive_task: asyncio.Task | None = None
        self._keepalive_enabled = False
        self._keepalive_interval = 15.0  # seconds between pings
        self._log = log_callback or (lambda msg: logger.info(msg))
        self._running = False

    # ── lifecycle ──────────────────────────────────────────

    def start(self):
        """Start the background asyncio event loop thread."""
        if self._running:
            return
        self._running = True

        def _run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._thread = threading.Thread(
            target=_run_loop, daemon=True, name="pikmin-tunnel-loop"
        )
        self._thread.start()
        while self._loop is None:
            time.sleep(0.01)
        self._log("[TunnelRegistry] event loop started")

    def stop(self):
        """Stop background loop and clean up all tunnels."""
        if not self._running:
            return
        self._running = False

        async def _cleanup():
            self._keepalive_enabled = False
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
                self._keepalive_task = None
            for wd in list(self._watchdog_tasks.values()):
                if not wd.done():
                    wd.cancel()
            self._watchdog_tasks.clear()
            await self.stop_all()

        if self._loop and self._loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(_cleanup(), self._loop)
                fut.result(timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None
        self._log("[TunnelRegistry] stopped")

    def _run_async(self, coro, timeout=30):
        """Run coroutine on background loop and return result (blocking)."""
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("Event loop not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ── tunnel CRUD ────────────────────────────────────────

    async def start_tunnel(self, udid: str, ip: str, port: int) -> dict:
        """Create and register a tunnel for the given device (single-shot, no fallback)."""
        async with self._lock:
            existing = self._tunnels.get(udid)
            if existing and existing.is_running():
                await existing.stop()
            runner = TunnelRunner()
            info = await runner.start(udid, ip, port)
            self._tunnels[udid] = runner
            self._log(
                f"[TunnelRegistry] tunnel started: {udid} → "
                f"{info['rsd_address']}:{info['rsd_port']}"
            )
            return info

    async def start_tunnel_with_candidates(
        self, user_udid: str, ip: str, port: int, per_candidate_timeout: float = 8.0,
    ) -> dict:
        """Try multiple UDID candidates against the same (ip, port).

        Pair-verify fails fast (~200-400ms) on a wrong identifier, so trying
        several is cheap.  TimeoutError is a hard stop (network unreachable).
        Matches locwarp's wifi_tunnel_start() candidate loop.

        Returns: {"udid": <winning_udid>, "rsd_address": ..., "rsd_port": ...,
                   "interface": ..., "protocol": ...}
        """
        from pymobiledevice3.exceptions import ConnectionTerminatedError

        candidates = _build_tunnel_udid_candidates(user_udid, ip, port)
        logger.info(
            "WiFi tunnel candidates: ip=%s port=%d candidates=%s",
            ip, port, candidates,
        )

        last_error: BaseException | None = None
        async with self._lock:
            for cand in candidates:
                existing = self._tunnels.get(cand)
                if existing is not None and existing.is_running():
                    if (existing.target_ip == ip and existing.target_port == port):
                        # Same target already running — idempotent re-click
                        if existing.info:
                            return {"udid": cand, **existing.info}
                        continue
                    # Different target, skip to avoid killing an active tunnel
                    logger.debug(
                        "Skipping candidate %s: already tunneling to %s:%s",
                        cand, existing.target_ip, existing.target_port,
                    )
                    continue
                if existing is not None:
                    # Stale entry — clean up
                    await existing.stop()
                    self._tunnels.pop(cand, None)

                runner = TunnelRunner()
                try:
                    info = await runner.start(cand, ip, port, timeout=per_candidate_timeout)
                except asyncio.TimeoutError as e:
                    last_error = e
                    logger.warning(
                        "WiFi tunnel timed out for udid=%s; network unreachable — stopping",
                        cand,
                    )
                    raise  # hard stop — next candidate would hit same wall
                except (ConnectionTerminatedError, asyncio.IncompleteReadError) as e:
                    last_error = e
                    logger.info(
                        "WiFi tunnel candidate %s failed (%s); trying next",
                        cand, type(e).__name__,
                    )
                    continue
                except BaseException as e:
                    last_error = e
                    logger.info(
                        "WiFi tunnel candidate %s failed (%s); trying next",
                        cand, type(e).__name__,
                    )
                    continue

                self._tunnels[cand] = runner
                self._log(
                    f"[TunnelRegistry] tunnel started: {cand} → "
                    f"{info['rsd_address']}:{info['rsd_port']}"
                )
                return {"udid": cand, **info}

        msg = f"All candidates failed: {last_error}" if last_error else "All candidates failed"
        raise RuntimeError(msg)

    def start_tunnel_sync(self, udid: str, ip: str, port: int, timeout: float = 25.0) -> dict:
        """Blocking wrapper with multi-UDID candidate fallback (safe from tkinter callbacks)."""
        return self._run_async(
            self.start_tunnel_with_candidates(udid, ip, port,
                per_candidate_timeout=min(8.0, timeout / 3)),
            timeout=timeout,
        )

    async def stop_tunnel(self, udid: str):
        """Stop and remove a tunnel."""
        async with self._lock:
            wd = self._watchdog_tasks.pop(udid, None)
            if wd and not wd.done():
                wd.cancel()
            runner = self._tunnels.pop(udid, None)
            if runner:
                await runner.stop()
                self._log(f"[TunnelRegistry] tunnel stopped: {udid}")

    def stop_tunnel_sync(self, udid: str):
        """Blocking wrapper for stop_tunnel."""
        try:
            self._run_async(self.stop_tunnel(udid), timeout=10)
        except Exception:
            pass

    async def stop_all(self):
        """Stop all tunnels."""
        async with self._lock:
            for udid in list(self._tunnels):
                await self.stop_tunnel(udid)

    def stop_all_sync(self):
        """Blocking wrapper for stop_all."""
        try:
            self._run_async(self.stop_all(), timeout=10)
        except Exception:
            pass

    def get_info(self, udid: str | None = None) -> dict | None:
        """Get RSD info for a tunnel. If udid is None, returns first active."""
        if udid:
            runner = self._tunnels.get(udid)
            return dict(runner.info) if (runner and runner.info) else None
        for runner in self._tunnels.values():
            if runner.info:
                return dict(runner.info)
        return None

    def get_first_udid(self) -> str | None:
        """Return the UDID of the first active tunnel."""
        for udid, runner in self._tunnels.items():
            if runner.is_running():
                return udid
        return None

    def is_any_tunnel_running(self) -> bool:
        """Check if any tunnel is currently active."""
        return any(r.is_running() for r in self._tunnels.values())

    # ── watchdog ───────────────────────────────────────────

    async def _watchdog(self, udid: str):
        """Monitor tunnel task; auto-restart on unexpected death.

        Stays in a loop: waits for runner.task to finish, then if the tunnel
        wasn't intentionally stopped, creates a new runner with the same
        (ip, port) and spawns a replacement watchdog.
        """
        while self._running:
            runner = self._tunnels.get(udid)
            if not runner or not runner.task:
                break

            try:
                await runner.task
            except asyncio.CancelledError:
                break
            except Exception:
                pass

            # Check if intentionally removed
            runner = self._tunnels.get(udid)
            if not runner:
                break

            ip = runner.target_ip
            port = runner.target_port
            if not (ip and port):
                self._log(f"[Watchdog] no target ip/port for {udid}; giving up")
                break

            self._log(f"[Watchdog] tunnel {udid} died; restarting in 3s...")
            await asyncio.sleep(3)

            try:
                async with self._lock:
                    current = self._tunnels.get(udid)
                    if current is not runner:
                        break  # replaced externally
                    new_runner = TunnelRunner()
                    await new_runner.start(udid, ip, port)
                    self._tunnels[udid] = new_runner
                    self._watchdog_tasks.pop(udid, None)
                    wd = asyncio.create_task(self._watchdog(udid))
                    self._watchdog_tasks[udid] = wd
                    self._log(f"[Watchdog] tunnel restarted: {udid}")
                    return  # old watchdog exits; new one takes over
            except Exception as e:
                self._log(f"[Watchdog] restart failed: {e}; retry in 8s...")
                await asyncio.sleep(8)

    async def start_watchdog(self, udid: str):
        """Start watchdog monitoring for a tunnel."""
        existing = self._watchdog_tasks.pop(udid, None)
        if existing and not existing.done():
            existing.cancel()
        wd = asyncio.create_task(self._watchdog(udid))
        self._watchdog_tasks[udid] = wd

    def start_watchdog_sync(self, udid: str):
        """Blocking wrapper for start_watchdog."""
        try:
            self._run_async(self.start_watchdog(udid), timeout=5)
        except Exception:
            pass

    # ── keepalive ──────────────────────────────────────────

    async def start_keepalive(self):
        """Periodically ping RSD to prevent tunnel idle timeout.

        Connects to each tunnel's RSD address, verifies the connection
        is alive, then disconnects. This lightweight exercise helps
        prevent the iOS device from dropping the WiFi tunnel.
        """
        if self._keepalive_enabled:
            return
        self._keepalive_enabled = True
        if self._keepalive_task and not self._keepalive_task.done():
            return

        async def _loop():
            while self._keepalive_enabled:
                await asyncio.sleep(self._keepalive_interval)
                async with self._lock:
                    for udid, runner in list(self._tunnels.items()):
                        if not runner.is_running() or not runner.info:
                            continue
                        try:
                            from pymobiledevice3.remote.remote_service_discovery import (
                                RemoteServiceDiscoveryService,
                            )
                            rsd = RemoteServiceDiscoveryService(
                                (runner.info["rsd_address"], runner.info["rsd_port"])
                            )
                            await asyncio.wait_for(rsd.connect(), timeout=5.0)
                            await asyncio.wait_for(rsd.close(), timeout=3.0)
                            logger.debug("Keepalive OK: %s", udid)
                        except Exception:
                            logger.debug("Keepalive fail: %s", udid)

        self._keepalive_task = asyncio.create_task(_loop())
        self._log("[Keepalive] started")

    async def stop_keepalive(self):
        """Stop periodic keepalive pings."""
        self._keepalive_enabled = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            self._keepalive_task = None
            self._log("[Keepalive] stopped")

    def start_keepalive_sync(self):
        """Blocking wrapper for start_keepalive."""
        try:
            self._run_async(self.start_keepalive(), timeout=5)
        except Exception:
            pass

    def stop_keepalive_sync(self):
        """Blocking wrapper for stop_keepalive."""
        try:
            self._run_async(self.stop_keepalive(), timeout=5)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# Discovery helpers (used by GUI for device scanning)
# ═══════════════════════════════════════════════════════════════

async def _get_primary_local_ip() -> str | None:
    """Return this machine's primary IPv4."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


async def _tcp_probe(ip: str, port: int, timeout: float = 0.3) -> bool:
    """Check if a TCP port is open on a remote host."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout,
        )
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _scan_subnet_for_port(port: int = 49152) -> list[str]:
    """Scan local /24 subnet for hosts responding on the given TCP port."""
    import socket as _socket
    my_ip = await _get_primary_local_ip()
    if not my_ip:
        return []
    prefix = ".".join(my_ip.split(".")[:3]) + "."
    tasks = [_tcp_probe(prefix + str(i), port) for i in range(1, 255)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    hits = []
    for i, ok in enumerate(results):
        if ok is True:
            hits.append(prefix + str(i + 1))
    return hits


def _load_pair_record_udids() -> tuple[str | None, list[str]]:
    """Read UDIDs from ~/.pymobiledevice3/ pair records.

    Returns (preferred_remote_udid, all_udids) where:
    - preferred_remote_udid: the UDID that also has a remote_ pair record
      (RemotePairing tunnelling requires this), or None
    - all_udids: all valid UDIDs found, with remote_ ones listed first

    Files named `remote_<UDID>.plist` are RemotePairing pair records,
    while `<UDID>.plist` are USB pair records.
    """
    from pathlib import Path as _Path
    base = _Path.home() / ".pymobiledevice3"
    if not base.is_dir():
        return None, []
    remote_udids: list[str] = []
    usb_udids: list[str] = []
    for entry in sorted(base.iterdir(), reverse=True):
        if not entry.suffix == ".plist":
            continue
        name = entry.stem
        is_remote = name.startswith("remote_")
        udid = name[len("remote_"):] if is_remote else name
        if all(c in "0123456789abcdefABCDEF-" for c in udid) and len(udid) >= 24:
            if is_remote:
                if udid not in remote_udids:
                    remote_udids.append(udid)
            else:
                if udid not in usb_udids and udid not in remote_udids:
                    usb_udids.append(udid)
        if len(remote_udids) + len(usb_udids) >= 5:
            break
    all_udids = remote_udids + usb_udids
    preferred = remote_udids[0] if remote_udids else (usb_udids[0] if usb_udids else None)
    return preferred, all_udids


def _build_tunnel_udid_candidates(user_udid: str, ip: str, port: int) -> list[str]:
    """Build a priority-ordered list of UDID candidates for tunnel start.

    Priority order (matching locwarp's _build_tunnel_udid_candidates):
    1. The UDID the user explicitly selected (always trusted)
    2. All UDIDs from ~/.pymobiledevice3/ pair records, with remote_ ones
       listed first (most recently modified first)

    The list is de-duplicated while preserving order. Caller iterates them;
    pair-verify fails fast (~200-400ms) on a wrong identifier so trying
    several is cheap.  TimeoutError is treated as a hard stop (network
    unreachable).
    """
    candidates: list[str] = []

    def _add(c: str | None) -> None:
        if c and c not in candidates:
            candidates.append(c)

    _add(user_udid)
    _, all_udids = _load_pair_record_udids()
    for u in all_udids:
        _add(u)

    if not candidates:
        candidates.append(f"pending:{ip}:{port}")
    return candidates


async def _scan_ports_for_ip(
    ip: str,
    start: int = 49152,
    end: int = 65535,
    concurrency: int = 1024,
    timeout: float = 0.35,
) -> list[int]:
    """Scan dynamic-range ports on a candidate IP.

    iOS picks its RemotePairing port from 49152-65535 at boot / network
    rebind. A full-range scan on a same-LAN host returns in a few seconds
    because closed ports issue RST immediately.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _probe_one(p: int) -> int | None:
        async with sem:
            ok = await _tcp_probe(ip, p, timeout)
            return p if ok else None

    tasks = [asyncio.create_task(_probe_one(p)) for p in range(start, end + 1)]
    hits: list[int] = []
    for fut in asyncio.as_completed(tasks):
        try:
            res = await fut
        except (OSError, ConnectionError, asyncio.TimeoutError):
            res = None
        if res is not None:
            hits.append(res)
    hits.sort()
    return hits


async def discover_remotepairing_devices() -> list[dict]:
    """Find iPhones via mDNS → TCP probe → port-range scan.

    Strategy:
      1) mDNS Bonjour broadcast (fastest, gives exact host:port)
      2) TCP probe on 62078 (lockdown) + 49152 /24 subnet → candidate IPs
      3) Port-range scan (49152-65535) on each candidate → exact RemotePairing port
      4) USB pair records → authoritative UDIDs

    Returns list of {udid, hostname, port, name, method}.
    """
    results: list[dict] = []

    # ── 0) Pre-load pair record UDIDs for later merging ──
    preferred_udid, known_udids = _load_pair_record_udids()

    # ── 1) mDNS / Bonjour broadcast ──
    try:
        from pymobiledevice3.bonjour import browse_remotepairing
        instances = await browse_remotepairing(timeout=3.0)
        for inst in instances:
            raw_addrs = inst.addresses or []
            str_addrs: list[str] = []
            for a in raw_addrs:
                if hasattr(a, "ip"):
                    str_addrs.append(str(a.ip))
                else:
                    str_addrs.append(str(a))
            ipv4s = [s for s in str_addrs if ":" not in s]
            addrs = ipv4s if ipv4s else str_addrs
            for addr in addrs:
                # Bonjour identifier is a UUID (not hardware UDID).
                # Leave udid empty; it will be filled from pair records below.
                results.append({
                    "udid": "",
                    "hostname": addr,
                    "port": inst.port,
                    "name": getattr(inst, "instance", "") or getattr(inst, "host", ""),
                    "method": "mdns",
                })
    except Exception as e:
        logger.warning("mDNS browse failed: %s", e)

    # ── 2) TCP probe /24 → candidate IPs (only when mDNS yielded nothing) ──
    candidates: set[str] = set()
    if not results:
        logger.info("mDNS empty; probing /24 subnet for candidates")
        try:
            for p in (62078, 49152):
                try:
                    hits = await _scan_subnet_for_port(p)
                    candidates.update(hits)
                except Exception as e:
                    logger.warning("Probe scan port %d failed: %s", p, e)
        except Exception as e:
            logger.warning("TCP probe scan failed: %s", e)

    # ── 3) Port-range scan each candidate for exact RemotePairing port ──
    #     Only when mDNS empty AND we have candidate IPs (avoids false positives
    #     from other LAN hosts' open ephemeral ports).
    if candidates and not results:
        logger.info("Port-range scanning %d candidate(s) ...", len(candidates))
        async def _scan_one(ip: str):
            try:
                ports = await _scan_ports_for_ip(ip)
                for port in ports:
                    results.append({
                        "udid": "",
                        "hostname": ip,
                        "port": port,
                        "name": ip,
                        "method": "tcp_scan",
                    })
            except Exception as e:
                logger.warning("Port scan for %s failed: %s", ip, e)

        await asyncio.gather(*[_scan_one(ip) for ip in candidates])

    # ── 4) Merge pair record UDIDs ──
    # Priority: remote_<UDID>.plist (has RemotePairing credentials) > USB-only
    if known_udids:
        for r in results:
            if not r["udid"] or r["udid"] == "":
                r["udid"] = preferred_udid or known_udids[0]

    # De-dupe on (hostname, port)
    seen = set()
    unique = []
    for r in results:
        key = (r["hostname"], r["port"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    return unique


async def get_usb_device_udid() -> str | None:
    """Get the UDID of the first connected USB device."""
    try:
        from pymobiledevice3.usbmux import list_devices
        devices = await list_devices()
        for d in devices:
            if getattr(d, "connection_type", "") == "USB":
                return getattr(d, "serial", "") or getattr(d, "udid", "")
    except Exception as e:
        logger.warning("USB device lookup failed: %s", e)
    return None


async def wifi_repair() -> dict:
    """Regenerate the RemotePairing pair record via USB.

    Requires iPhone connected via USB. The iPhone will show a 'Trust This
    Computer' prompt; after tapping 信任, a fresh RemotePairing record is
    written to ~/.pymobiledevice3/ so WiFi Tunnel works again.

    Returns: {"status": "paired", "udid": ..., "name": ..., "ios_version": ...}
    """
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.usbmux import list_devices as mux_list_devices
    from pymobiledevice3.remote.tunnel_service import (
        CoreDeviceTunnelProxy,
        create_core_device_tunnel_service_using_rsd,
    )
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pathlib import Path as _Path

    raw_devices = await mux_list_devices()
    usb_dev = next((d for d in raw_devices if getattr(d, "connection_type", "USB") == "USB"), None)
    if usb_dev is None:
        raise RuntimeError("請先用 USB 線連接 iPhone。重新配對需要 USB 觸發「信任這台電腦」提示。")

    udid = usb_dev.serial
    logger.info("Re-pair requested for USB device %s", udid)

    # Step 1: USB lockdown autopair — pops Trust prompt if USB record missing
    lockdown = await create_using_usbmux(serial=udid, autopair=True)
    ios_version = lockdown.all_values.get("ProductVersion", "0.0")
    name = lockdown.all_values.get("DeviceName", "iPhone")

    # Step 2: iOS 17+ — regenerate RemotePairing record via tunnel handshake
    try:
        major = int(ios_version.split(".")[0])
    except (ValueError, IndexError):
        major = 0

    remote_record_regenerated = False
    if major >= 17:
        # Delete stale remote pair record so it actually re-pairs
        try:
            from pymobiledevice3.common import get_home_folder
            from pymobiledevice3.pair_records import (
                PAIRING_RECORD_EXT,
                get_remote_pairing_record_filename,
            )
            stale = _Path(get_home_folder()) / f"{get_remote_pairing_record_filename(udid)}.{PAIRING_RECORD_EXT}"
            if stale.exists():
                stale.unlink()
                logger.info("Re-pair: removed stale remote pair record %s", stale)
        except Exception:
            logger.debug("Re-pair: could not check/remove stale pair record", exc_info=True)

        proxy = None
        tunnel_ctx = None
        rsd = None
        tunnel_svc = None
        try:
            proxy = await CoreDeviceTunnelProxy.create(lockdown)
            tunnel_ctx = proxy.start_tcp_tunnel()
            tunnel_result = await tunnel_ctx.__aenter__()
            rsd = RemoteServiceDiscoveryService((tunnel_result.address, tunnel_result.port))
            await rsd.connect()
            logger.info(
                "Re-pair: opening CoreDeviceTunnelService over RSD %s:%s — "
                "Trust prompt should appear on iPhone...",
                tunnel_result.address, tunnel_result.port,
            )
            tunnel_svc = await create_core_device_tunnel_service_using_rsd(rsd, autopair=True)
            logger.info("Re-pair: CoreDeviceTunnelService connected — RemotePairing record written")
            remote_record_regenerated = True
        except Exception as e:
            msg = str(e)
            if "PairingDialogResponsePending" in msg or "consent" in msg.lower():
                raise RuntimeError("請在 iPhone 解鎖螢幕上按「信任」後重試 (timeout 只有幾秒)。")
            elif "not paired" in msg.lower() or "pairingerror" in msg.lower():
                raise RuntimeError("USB 配對失效，請拔 USB 重插一次並按信任。")
            else:
                raise RuntimeError(f"RemotePairing 握手失敗: {msg}")
        finally:
            for closer in (
                lambda: tunnel_svc and tunnel_svc.close(),
                lambda: rsd and rsd.close(),
                lambda: tunnel_ctx and tunnel_ctx.__aexit__(None, None, None),
            ):
                try:
                    r = closer()
                    if hasattr(r, "__await__"):
                        await r
                except Exception:
                    pass
            try:
                if proxy is not None:
                    proxy.close()
            except Exception:
                pass

    return {
        "status": "paired",
        "udid": udid,
        "name": name,
        "ios_version": ios_version,
        "remote_record_regenerated": remote_record_regenerated,
    }
