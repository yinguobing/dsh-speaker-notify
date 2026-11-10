#!/usr/bin/env python3
"""speakd —— 常驻的"客厅喇叭"。热着 TTS 模型，独占音箱，对外只有一个 unix socket。

为什么要有它：Kokoro 冷启动 6.7 秒（torch import + 327MB 权重），通知的价值在于
"立刻响"，所以模型必须一直热着。它不顺带做别的：不做 MCP、不开公网端口、不管调度。

协议：unix socket，一行一个 JSON 请求，一行一个 JSON 回复。
  {"text": "任务完成", "volume": 40}
  {"ok": true, "audio": 3.2, "started": true}

用法：
  speakd.py --socket /run/user/1000/speakd.sock --voice zf_001
"""
import argparse
import json
import os
import queue
import socketserver
import sys
import tempfile
import threading
import time
from datetime import datetime, time as dtime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_SPEAKER = os.environ.get("SPEAKD_SPEAKER", "")

STATE_DIR = Path(os.environ.get("SPEAKD_STATE", Path.home() / ".local/state/speakd"))
LOG = STATE_DIR / "notify.log"
AUDIO_DIR = STATE_DIR / "audio"


def log(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- DLNA 传输层
class Speaker:
    """对客厅那台华为 SOUND X 的薄封装。复用 tools/dlna/soundx.py 的 SOAP 实现。"""

    def __init__(self, soundx_dir: Path, endpoint: str = ""):
        sys.path.insert(0, str(soundx_dir))
        import soundx  # noqa: E402

        # 把配置的音箱地址塞进传输层（它读 SOUNDX_HOST/SOUNDX_PORT 两个全局）
        if endpoint:
            host, _, port = endpoint.partition(":")
            soundx.HOST = host
            if port:
                soundx.PORT = int(port)
            soundx.BASE = f"{soundx.HOST}:{soundx.PORT}" if False else f"http://{soundx.HOST}:{soundx.PORT}"
        self._s = soundx

    def reachable(self) -> bool:
        try:
            self._s.status()
            return True
        except Exception:
            return False

    def set_volume(self, v: int) -> None:
        self._s.set_volume(max(0, min(100, int(v))))

    def play(self, url: str, title: str, duration: float | None) -> None:
        self._s.play_url(url, title=title, mime="audio/wav", duration=duration, keep=1.0)

    def release(self) -> str:
        """播完把媒体清掉，别让音箱攥着我们的 URL 不放。

        不清理的后果（实测）：DLNA 放完停在 STOPPED 但 CurrentURI 还指着我们的 wav，
        音箱的物理播放键会把刚才那条通知重播一遍，灯也常亮，蓝牙源回不去。
        """
        try:
            self._s._soap("Stop", "<InstanceID>0</InstanceID>", self._s.AVT, "rendertransport1")
        except SystemExit:
            pass
        try:
            self._s._soap(
                "SetAVTransportURI",
                "<InstanceID>0</InstanceID><CurrentURI></CurrentURI><CurrentURIMetaData></CurrentURIMetaData>",
                self._s.AVT, "rendertransport1",
            )
        except SystemExit as e:
            return f"clear-uri failed: {e}"
        try:
            info = self._s._soap("GetTransportInfo", "<InstanceID>0</InstanceID>", self._s.AVT, "rendertransport1")
            return self._s._field(info, "CurrentTransportState") or "unknown"
        except SystemExit:
            return "unknown"


class Presence:
    """人在不在电脑前 —— 只信真的测得到的信号。

    试过但放弃：logind 的 IdleHint（实测说 34 分钟无活动，而人正在聊天）。
    现在用 GNOME/Mutter 的 IdleMonitor（D-Bus）：它直接反映键鼠输入的空闲毫秒数，
    实测 20ms / 2.5s / 30s 随动作变化，是真的。
    拿不到就返回 unknown，按"走开了"处理 —— 宁可吵到客厅，也别让人错过敏报。
    """

    def __init__(self, window_s: float = 120):
        self.window_ms = int(window_s * 1000)

    def _env(self) -> dict:
        import subprocess  # noqa: F401

        env = os.environ.copy()
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
        return env

    def idle_ms(self) -> int | None:
        import re
        import subprocess

        try:
            out = subprocess.run(
                ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                 "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                 "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
                capture_output=True, text=True, timeout=3, env=self._env(),
            ).stdout
            m = re.search(r"uint64 (\d+)", out)
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def screen_locked(self) -> bool | None:
        import subprocess

        try:
            out = subprocess.run(["loginctl", "list-sessions", "--no-legend"],
                                 capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():
                f = line.split()
                if len(f) < 3 or f[1] != str(os.getuid()):
                    continue
                info = subprocess.run(["loginctl", "show-session", f[0], "-p", "Type", "-p", "LockedHint"],
                                      capture_output=True, text=True, timeout=3).stdout
                if "Type=wayland" not in info and "Type=x11" not in info:
                    continue
                return "LockedHint=yes" in info
            return None
        except Exception:
            return None

    def probe(self) -> tuple[str, dict]:
        locked = self.screen_locked()
        if locked:
            return "away", {"signal": "locked", "locked": True}
        idle = self.idle_ms()
        if idle is None:
            return "unknown", {"signal": "none"}
        state = "present" if idle < self.window_ms else "away"
        return state, {"signal": "mutter-idle", "idle_ms": idle, "window_ms": self.window_ms}


class LocalSink:
    """本机声卡出口：显示器自带喇叭或插着的耳机。零网络依赖，立刻出声。"""

    def play(self, path: Path, volume: int) -> None:
        import subprocess

        v = int(max(0, min(100, volume)) / 100 * 65536)
        proc = subprocess.run(
            ["paplay", f"--volume={v}", str(path)],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"paplay 失败: {proc.stderr.strip()[:200]}")


# ---------------------------------------------------------------- TTS
class Kokoro:
    def __init__(self, voice: str, threads: int):
        import numpy as np
        import torch
        from kokoro import KPipeline

        torch.set_num_threads(threads)
        t0 = time.time()
        self.pipe = KPipeline(lang_code="z", repo_id="hexgrad/Kokoro-82M-v1.1-zh")
        self._np = np
        self.init_seconds = time.time() - t0
        self.default_voice = voice
        self._lock = threading.Lock()  # torch 推理不并发，串起来

    def synth(self, text: str, voice: str, path: Path) -> float:
        import soundfile as sf

        with self._lock:
            chunks = [audio for _, _, audio in self.pipe(text, voice=voice or self.default_voice, speed=1.0)]
        if not chunks:
            raise RuntimeError("TTS 没产出音频")
        audio = self._np.concatenate(chunks)
        sf.write(str(path), audio, 24000)
        return len(audio) / 24000


# ---------------------------------------------------------------- 音频 HTTP 服务
class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def start_audio_server(directory: Path, port: int) -> ThreadingHTTPServer:
    import functools

    handler = functools.partial(_QuietHandler, directory=str(directory))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def lan_ip(host: str) -> str:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host.split(":")[0], int(host.split(":")[1])))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------- 服务主体
class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        self.tts = Kokoro(cfg.voice, cfg.threads)
        self.speaker = Speaker(Path(cfg.soundx_dir), cfg.speaker)
        self.local = LocalSink()
        self.presence = Presence(getattr(cfg, "present_window", 120))
        self.http = start_audio_server(AUDIO_DIR, cfg.audio_port)
        self.base = f"http://{lan_ip(cfg.speaker)}:{cfg.audio_port}"
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def apply_ceiling(self, requested: int | None) -> tuple[int, int | None]:
        """音量安全阀。不是替调用方做决定：超限就夹住，并在回执里说明夹过。"""
        if requested is None:
            return self.cfg.max_volume, None
        v = int(requested)
        if v > self.cfg.max_volume:
            return self.cfg.max_volume, v
        return max(0, v), None

    def _worker(self):
        while True:
            job = self.q.get()
            try:
                job()
            except Exception as e:  # 一个请求失败不能拖死守护进程
                log({"level": "error", "error": repr(e)})
            finally:
                self.q.task_done()

    def handle(self, req: dict) -> dict:
        if req.get("probe_presence"):
            state, info = self.presence.probe()
            return {"ok": True, "presence": state, "detail": info}

        text = (req.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "空文本"}
        if len(text) > 300:
            return {"ok": False, "error": "文本超过 300 字，通知不该这么长"}
        if req.get("kind", "notify") not in ("notify", "test"):
            return {"ok": False, "error": "kind 只能是 notify/test"}

        # 出口由调用方决定。不给就是错，不替他猜。
        sink = req.get("sink")
        if sink not in ("local", "living"):
            return {"ok": False, "error": "sink 必填：local | living（出口是调用方的决定）"}

        mode = req.get("mode", "speak")
        if mode not in ("speak", "log-only"):
            return {"ok": False, "error": "mode 只能是 speak / log-only"}

        if mode == "log-only":
            log({"level": "suppressed", "text": text, "reason": req.get("reason") or "caller",
                 "sink": sink})
            return {"ok": True, "logged": True, "mode": mode}

        voice = req.get("voice") or self.cfg.voice
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = AUDIO_DIR / f"{stamp}.wav"
        t0 = time.time()
        duration = self.tts.synth(text, voice, path)
        synth_seconds = time.time() - t0

        volume, clamped_from = self.apply_ceiling(req.get("volume"))
        fallback = bool(req.get("fallback"))
        url = f"{self.base}/{path.name}"
        self_ref = self

        def play():
            # 不擅自换出口：只有调用方明确要求 fallback 时才会去试另一个
            order = [sink] + (["local" if sink == "living" else "living"] if fallback else [])
            last_err = None
            for attempt, target in enumerate(order):
                try:
                    if target == "local":
                        self_ref.local.play(path, volume)
                    else:
                        self_ref.speaker.set_volume(volume)
                        self_ref.speaker.play(url, title="notify", duration=duration)
                    state = "n/a"
                    if target == "living" and self_ref.cfg.release:
                        state = self_ref.speaker.release()
                    log({"level": "info", "text": text, "voice": voice, "volume": volume,
                         "sink": target, "fallback": attempt > 0,
                         "clamped_from": clamped_from, "released": state,
                         "synth_s": round(synth_seconds, 2), "audio_s": round(duration, 2)})
                    return
                except Exception as e:
                    last_err = f"{target}: {e!r}"
                    log({"level": "error", "text": text, "sink": target, "error": repr(e)})
            log({"level": "error", "text": text, "sink": sink, "error": last_err})

        self.q.put(play)
        return {"ok": True, "queued": self.q.qsize(), "sink": sink,
                "synth_s": round(synth_seconds, 2), "audio_s": round(duration, 2),
                "volume": volume, "clamped_from": clamped_from, "fallback": fallback}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(65536)
        if not line:
            return
        try:
            req = json.loads(line.decode("utf-8"))
            resp = self.server.body.handle(req)  # type: ignore[attr-defined]
        except Exception as e:
            resp = {"ok": False, "error": f"bad request: {e!r}"}
        self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())


class Server_(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default=os.environ.get("SPEAKD_SOCKET", f"/run/user/{os.getuid()}/speakd.sock"))
    ap.add_argument("--voice", default="zm_055")
    ap.add_argument("--no-release", dest="release", action="store_false",
                    help="播完不要清空音箱的 URI（默认会清，避免播放键重播旧通知）")
    ap.add_argument("--present-window", type=float, default=300,
                    help="只用于 probe_presence 的测量口径，不影响播报决策")
    ap.add_argument("--max-volume", type=int, default=45)


    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--audio-port", type=int, default=8790)
    ap.add_argument("--speaker", default=DEFAULT_SPEAKER,
                    help="DLNA renderer 的 host:port，或用 SPEAKD_SPEAKER 环境变量。不给就只能走本机声卡")
    ap.add_argument("--soundx-dir", default=str(Path(__file__).resolve().parent))
    cfg = ap.parse_args()


    t0 = time.time()
    srv = Server(cfg)
    print(f"speakd ready in {time.time()-t0:.1f}s (tts load {srv.tts.init_seconds:.1f}s), "
          f"socket={cfg.socket}, audio={srv.base}", flush=True)

    sock = cfg.socket
    if os.path.exists(sock):
        # 只在确认没有别的实例占着时才删
        import socket as _s

        probe = _s.socket(_s.AF_UNIX)
        try:
            probe.connect(sock)
            print(f"另一个 speakd 已占用 {sock}，退出", file=sys.stderr)
            sys.exit(1)
        except OSError:
            os.unlink(sock)
        finally:
            probe.close()

    with Server_(sock, Handler) as server:
        server.body = srv  # type: ignore[attr-defined]
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            os.unlink(sock)


if __name__ == "__main__":
    main()
