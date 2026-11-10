# dsh-speaker-notify

让 [DSH](https://github.com/deepseek-ai/deepseek-harness)（DeepSeek Harness）里的 agent 通过音箱说话：
一个 `speak` 工具，加上**后台长任务落定时自动播报**。

起因很具体：跑长任务时人不在电脑前，任务结束了没人知道。屏幕上的通知对走开的人无效，声音才有效。

```
┌──────────────┐   speak 工具 / job/settled 事件
│ DSH 插件      │──────────────────────────────┐
│ （策略层）    │  决定：说不说、从哪说        │
└──────────────┘                              ▼
                                    ┌──────────────────┐
                                    │ notify（CLI）    │
                                    └──────────────────┘
                                              │ unix socket
                                              ▼
                                    ┌──────────────────┐
                                    │ speakd（常驻）   │
                                    │ TTS + 队列 + 播放│
                                    └──────────────────┘
                                       │            │
                              DLNA 音箱          本机声卡
```

## 两个出口

| 出口 | 实现 | 典型场景 |
|---|---|---|
| `living` | DLNA / UPnP MediaRenderer（如华为 SOUND X、绝大多数智能音箱） | 人走开了 |
| `local` | 本机声卡（`paplay`） | 人就坐在电脑前，不该惊动客厅 |

## 设计上的一条硬线：策略只在插件里

`speakd` 是**执行器**，不做任何决定。请求里 `sink` 必填；它不猜出口、不静默改道、不在深夜偷偷闭嘴。

```
{ "text": "...", "sink": "local" | "living", "mode": "speak" | "log-only", "volume": 45 }
```

`mode: "log-only"` 用来表达"这条只记不念"——由调用方决定，daemon 只照做。
唯一的例外是音量天花板：超了会夹住，但**回执和日志里写明**（`clamped_from`），不静默。

这么切的理由：工具一旦有隐藏策略，调用方就没法预测它的行为。"要不要响"属于调用方。

## 两种发起路径

| 路径 | 谁决定 | 需要模型记得吗 |
|---|---|---|
| `speak` 工具 | 模型（语义判断：值不值得打扰人） | 需要 |
| `job/settled` 钩子 | 代码（长任务落定必报） | 不需要 |

第二条是重点：长任务结束时回合早就结束了，模型不在场，只有事件钩子能不靠它记得。

## 安装

### 1. 常驻进程 speakd

需要 Python 3.11/3.12 + CPU 版 torch（Kokoro 要求 Python < 3.13）。

```bash
python3 -m venv ~/.venvs/speakd
~/.venvs/speakd/bin/pip install kokoro soundfile "misaki[zh]" torch --index-url https://download.pytorch.org/whl/cpu
```

克隆本仓库后：

```bash
# 先试跑（--speaker 是你的 DLNA 音箱 host:port，可从 SSDP 发现）
~/.venvs/speakd/bin/python daemon/speakd.py --speaker 192.168.1.50:49494
```

systemd 用户服务：

```ini
# ~/.config/systemd/user/speakd.service
[Unit]
Description=speakd — TTS speaker daemon
After=network-online.target

[Service]
ExecStart=%h/path/to/dsh-speaker-notify/daemon/speakd.py
Restart=always
Environment=SPEAKD_SPEAKER=192.168.1.50:49494

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now speakd
```

CLI 可以链到 PATH：`ln -s $PWD/daemon/notify ~/.local/bin/notify`

### 2. 装进 DSH profile

`~/.dsh/profiles/<profile>/package.json`：

```json
{ "dependencies": { "dsh-speaker-notify": "file:/path/to/dsh-speaker-notify" } }
```

`~/.dsh/profiles/<profile>/cordis.patch.yml`（放进 agent preset 的 `plugins` 列表）：

```yaml
- id: tool-speaker-notify
  name: 'dsh-speaker-notify'
  config:
    minSeconds: 300
    awayHours: 3
    quietFrom: '23:30'
    quietTo: '08:00'
```

然后 `pnpm install` 并重启 DSH（新插件行不是配置改动，`patchReload: live` 不会加载它）。

## 配置

| 键 | 默认 | 含义 |
|---|---|---|
| `notifyBin` | `notify` | CLI 路径（不在 PATH 里就写绝对路径） |
| `minSeconds` | `300` | 成功任务跑满多久才播报 |
| `autoAnnounce` | `true` | 关掉即不自动播报（`speak` 工具仍在） |
| `volume` | `0` | 0 = 用 speakd 默认；上限由 speakd 夹 |
| `quietFrom` / `quietTo` | `23:30` / `08:00` | 这段时间只记日志不出声 |
| `presentSeconds` | `300` | 键鼠空闲小于此值算人在电脑前 |
| `awayHours` | `3` | 连续这么久没人 → 只记日志（0 关闭） |

## 命令行

```bash
notify "任务完成"                     # 说一句
notify --sink local "只在这边说"      # 指定出口
notify --mode log-only --reason quiet "只记不念"
notify-run --label "转数据集" -- make  # 包住命令，成功/失败/中断都报
notify presence                       # 键鼠空闲多久（测量，不做决定）
notify log -n 20                      # 播报记录，含被压掉的
```

## 已知限制

- **内存**：Kokoro + torch 常驻约 1.8GB RSS。CPU 上 RTF ≈ 0.36（4.9 秒音频生成 1.8 秒）。
- **在场判定**：只在 GNOME（Mutter D-Bus `IdleMonitor`）上有效，其他桌面会退化成"测不到 → 走客厅"。
  `logind` 的 `IdleHint` 实测不可信，已弃用。
- **独占**：DLNA 播放期间音箱被占，语音助手/手机投屏会被顶掉。
- **局域网**：DLNA 出口要求同网段；音箱换 IP 要改配置。
- 单条文本上限 300 字。

## 许可

MIT
