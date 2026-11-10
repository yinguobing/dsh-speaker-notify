#!/usr/bin/env python3
"""推动客厅那台华为 SOUND X 出声（DLNA / UPnP MediaRenderer）。

用法：
  soundx.py status              # 当前状态 + 音量
  soundx.py vol 35              # 设音量
  soundx.py say "要说的英文"     # flite 合成语音播出去
  soundx.py play 文件.wav|mp3    # 播本地文件（自动起本地 HTTP 服务）
  soundx.py stop | pause | resume

原理：DLNA renderer（DMR-1.50）在本机 HTTP 上挂一个音频 URL，
AVTransport 的 SetAVTransportURI 指过去，音箱自己来拉流。

环境变量：SOUNDX_HOST / SOUNDX_PORT 指定 renderer。
"""
import argparse
import functools
import http.server
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import xml.sax.saxutils as sx

HOST = os.environ.get("SOUNDX_HOST", "")
PORT = int(os.environ.get("SOUNDX_PORT", "49494"))
BASE = f"http://{HOST}:{PORT}"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RCS = "urn:schemas-upnp-org:service:RenderingControl:1"
CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"


def _require_host() -> None:
    if not HOST:
        raise SystemExit("没配置音箱地址：设 SOUNDX_HOST（如 192.168.1.50），或用 --speaker 传给 speakd")


def _soap(action, body, svc, ctrl, retries=3):
    env = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{svc}">{body}</u:{action}></s:Body></s:Envelope>'
    )
    _require_host()
    req = urllib.request.Request(
        f"{BASE}/upnp/control/{ctrl}",
        data=env.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{svc}#{action}"',
        },
    )
    last = None
    for i in range(retries):
        try:
            return urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode("utf-8", "ignore")
            # 500/501 常见于音箱正在切状态，等一下重试
            if e.code >= 500 and i < retries - 1:
                last = f"HTTP {e.code}: {body_txt[:200]}"
                time.sleep(0.8)
                continue
            raise SystemExit(f"[{action}] HTTP {e.code}: {body_txt[:500]}")
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
            time.sleep(0.8)
    raise SystemExit(f"[{action}] 连不上音箱: {last}")


def _field(xml, name):
    m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S)
    return m.group(1) if m else None


def status():
    info = _soap("GetTransportInfo", "<InstanceID>0</InstanceID>", AVT, "rendertransport1")
    pos = _soap("GetPositionInfo", "<InstanceID>0</InstanceID>", AVT, "rendertransport1")
    vol = _soap("GetVolume", "<InstanceID>0</InstanceID><Channel>Master</Channel>", RCS, "rendercontrol1")
    print(f"state  : {_field(info, 'CurrentTransportState')}")
    print(f"volume : {_field(vol, 'CurrentVolume')}")
    uri = _field(pos, "TrackURI") or ""
    print(f"uri    : {uri[:120]}")
    print(f"pos    : {_field(pos, 'RelTime')} / {_field(pos, 'TrackDuration')}")


def set_volume(v):
    v = max(0, min(100, int(v)))
    _soap(
        "SetVolume",
        f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{v}</DesiredVolume>",
        RCS,
        "rendercontrol1",
    )
    print(f"volume -> {v}")


def _wait(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            info = _soap("GetTransportInfo", "<InstanceID>0</InstanceID>", AVT, "rendertransport1")
            if _field(info, "CurrentTransportState") in ("STOPPED", "NO_MEDIA_PRESENT"):
                return
        except SystemExit:
            return
        time.sleep(0.5)


def play_url(url, title="soundx", mime="audio/wav", duration=None, keep=1.5):
    meta = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{sx.escape(title)}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res protocolInfo="http-get:*:{mime}:*">{sx.escape(url)}</res>'
        "</item></DIDL-Lite>"
    )
    _soap(
        "SetAVTransportURI",
        f"<InstanceID>0</InstanceID><CurrentURI>{sx.escape(url)}</CurrentURI>"
        f"<CurrentURIMetaData>{sx.escape(meta)}</CurrentURIMetaData>",
        AVT,
        "rendertransport1",
    )
    _soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>", AVT, "rendertransport1")
    # 音箱切状态有延迟，别一看没 PLAYING 就判死
    state = None
    for _ in range(10):
        info = _soap("GetTransportInfo", "<InstanceID>0</InstanceID>", AVT, "rendertransport1")
        state = _field(info, "CurrentTransportState")
        if state in ("PLAYING", "TRANSITIONING"):
            break
        time.sleep(0.4)
    if state not in ("PLAYING", "TRANSITIONING"):
        raise SystemExit(f"没起来：{state}")
    if duration:
        _wait(duration + keep)


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((HOST, PORT))
        return s.getsockname()[0]
    finally:
        s.close()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve_dir(directory):
    handler = functools.partial(_Quiet, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://{_lan_ip()}:{httpd.server_address[1]}"


def play_file(path, keep=1.5):
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise SystemExit(f"没有这个文件：{path}")
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    ).stdout.strip()
    duration = float(out) if out else None
    mime = "audio/mpeg" if path.lower().endswith(".mp3") else "audio/wav"
    httpd, base = serve_dir(os.path.dirname(path))
    try:
        play_url(f"{base}/{os.path.basename(path)}", os.path.basename(path), mime, duration, keep)
        print(f"played {os.path.basename(path)} ({duration}s)" if duration else "playing")
    finally:
        httpd.shutdown()


def say(text, voice="slt", volume=None):
    tmp = tempfile.mkdtemp(prefix="soundx-")
    wav = os.path.join(tmp, "say.wav")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"flite=text='{text}':voice={voice}", "-ar", "48000", "-ac", "2", wav],
        check=True,
    )
    if volume is not None:
        set_volume(volume)
    play_file(wav)


def main():
    p = argparse.ArgumentParser(description="客厅华为 SOUND X 发声控制")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    v = sub.add_parser("vol"); v.add_argument("value", type=int)
    s = sub.add_parser("say")
    s.add_argument("text")
    s.add_argument("--voice", default="slt")
    s.add_argument("--vol", type=int)
    f = sub.add_parser("play"); f.add_argument("file")
    for c in ("stop", "pause", "resume"):
        sub.add_parser(c)
    a = p.parse_args()
    if a.cmd == "status":
        status()
    elif a.cmd == "vol":
        set_volume(a.value)
    elif a.cmd == "say":
        say(a.text, a.voice, a.vol)
    elif a.cmd == "play":
        play_file(a.file)
    elif a.cmd in ("stop", "pause", "resume"):
        act = {"stop": ("Stop", AVT, "rendertransport1"),
               "pause": ("Pause", AVT, "rendertransport1"),
               "resume": ("Play", AVT, "rendertransport1")}[a.cmd]
        body = "<InstanceID>0</InstanceID>" if act[0] != "Play" else "<InstanceID>0</InstanceID><Speed>1</Speed>"
        _soap(act[0], body, act[1], act[2])
        print(a.cmd, "ok")


if __name__ == "__main__":
    main()
