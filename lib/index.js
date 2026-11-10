// @xiaohei/dsh-speaker-notify — 让 agent 通过客厅音箱或电脑声卡说话。
//
// 分工（2026-11-08 定稿）：
// - **策略全在这里**：什么时候说、从哪个出口说、深夜要不要说。规则只有一份，看得见、可配。
// - **speakd 只是执行器**：给它 sink 就照播，不给 sink 就报错。它不猜、不改道、不静默。
// - `presence` 只是测量（键鼠空闲多少毫秒），不是决定；决定在这里做。
//
// 两条发起路径，性质不同：
// 1. `speak` 工具 —— 模型决定要不要说（语义判断）。
// 2. `job/settled` 钩子 —— 代码决定，长任务落定必报，模型不在场也成立。

import { execFile, spawn } from "node:child_process";
import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";

/** 稳定的 Cordis 插件名。 */
export const name = "speaker-notify";

export const Config = z.object({
  /** notify CLI 的绝对路径。 */
  notifyBin: z.string().default("notify"),
  /** 关掉就不再自动播报任何东西（`speak` 工具仍然可用）。 */
  autoAnnounce: z.boolean().default(true),
  /** 只有跑满这么多秒的任务才值得打扰他。 */
  minSeconds: z.number().default(300),
  /** 播报音量；0 表示用 speakd 的默认值（speakd 有 45 的硬天花板，超了会在回执里说明）。 */
  volume: z.number().default(0),
  /** 静音时段：这段时间只记日志不出声。留空则不禁声。 */
  quietFrom: z.string().default("23:30"),
  quietTo: z.string().default("08:00"),
  /** 键鼠空闲小于这么多秒，算人在电脑前 → 走本机声卡。 */
  presentSeconds: z.number().default(300),
  /** 连续这么久没碰键鼠，算人不在家 → 只记日志。0 表示不判定。 */
  awayHours: z.number().default(3),
});

export const inject = ["tools"];

/** 后台任务的 label 有时是一整条命令，念出来很傻；这里只留可读的开头。 */
const CMD_LIKE = /^(cat|echo|bash|sh|zsh|python|python3|node|npm|pnpm|yarn|make|cmake|curl|wget|ffmpeg|git|docker|uv|pip|rsync|tar)\b/;

function cleanLabel(raw) {
  let s = String(raw || "").replace(/\s+/g, " ").trim();
  s = s.split("<<")[0].split("&&")[0].split("|")[0].split(";")[0].trim();
  if (!s || CMD_LIKE.test(s) || /[<>]/.test(s)) return "后台任务";
  return s.length > 24 ? `${s.slice(0, 24)}…` : s;
}

/** 中文时长：42 分钟、3 小时 5 分、不到 1 秒。 */
function fmtDuration(seconds) {
  const s = Math.round(seconds);
  if (s < 1) return "不到 1 秒";
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m} 分 ${s % 60} 秒` : `${m} 分钟`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} 小时 ${m % 60} 分` : `${h} 小时`;
}

function inQuietHours(from, to, now = new Date()) {
  if (!from || !to) return false;
  const [fh, fm] = from.split(":").map(Number);
  const [th, tm] = to.split(":").map(Number);
  const cur = now.getHours() * 60 + now.getMinutes();
  const a = fh * 60 + fm;
  const b = th * 60 + tm;
  return a <= b ? cur >= a && cur < b : cur >= a || cur < b; // 后一种是跨午夜
}

/**
 * 插件装配。
 * @param {import("@deepseek-ai/cordis").Context} ctx
 */
export function apply(ctx, config) {
  const bin = config.notifyBin;
  const volume = Math.max(0, Math.min(45, Number(config.volume) || 0));

  /** 问 speakd 键鼠空闲多少 —— 纯测量，结果怎么用由这里决定。 */
  function probePresence() {
    return new Promise((resolve) => {
      execFile(bin, ["presence", "--json"], { timeout: 5000 }, (err, stdout) => {
        if (err) return resolve({ state: "unknown", idleMs: null });
        try {
          const j = JSON.parse(stdout);
          resolve({ state: j.presence ?? "unknown", idleMs: j.detail?.idle_ms ?? null });
        } catch {
          resolve({ state: "unknown", idleMs: null });
        }
      });
    });
  }

  /**
   * 策略：唯一的决定点。
   * 返回 {speak: boolean, sink, reason?}，全部可预测、可解释。
   */
  async function decide() {
    if (inQuietHours(config.quietFrom, config.quietTo)) {
      return { speak: false, sink: "living", reason: "quiet" };
    }
    const { state, idleMs } = await probePresence();
    const awayMs = config.awayHours > 0 ? config.awayHours * 3600 * 1000 : Infinity;
    if (idleMs !== null && idleMs >= awayMs) {
      return { speak: false, sink: "living", reason: "away" };
    }
    if (state === "present") {
      return { speak: true, sink: "local" };
    }
    return { speak: true, sink: "living" };
  }

  /** 发一条请求给 speakd；失败只记日志，绝不打断 agent 的回合。 */
  function send(text, { sink, mode = "speak", reason, fallback = false } = {}) {
    const args = ["--sink", sink, "--mode", mode];
    if (volume > 0) args.push("--volume", String(volume));
    if (reason) args.push("--reason", reason);
    if (fallback) args.push("--fallback");
    args.push(text);
    try {
      const child = spawn(bin, args, { stdio: "ignore", detached: true });
      child.on("error", (e) => ctx.logger?.warn?.(`[speaker] 播报失败: ${e.message}`));
      child.unref();
    } catch (e) {
      ctx.logger?.warn?.(`[speaker] 播报异常: ${String(e)}`);
    }
  }

  /** 按策略说一句（或只记不念）。 */
  async function speakByPolicy(text) {
    const d = await decide();
    send(text, d.speak ? { sink: d.sink } : { sink: d.sink, mode: "log-only", reason: d.reason });
  }

  // ---------------------------------------------------------------- 主动：工具
  ctx.tools.register(
    defineTool({
      name: "speak",
      description:
        "让用户听见一句中文话（你判断他可能不在屏幕前时用）。出口和静音由插件策略决定："
        + "他刚动过键鼠 → 电脑声卡（显示器喇叭/耳机），走开了 → 客厅音箱；深夜或长时间无人则只记日志不发声。"
        + "只在真需要他听见时调用：长任务结束、需要他拍板、出错要人处理。"
        + "一句 8-12 秒，念任务名、状态、耗时、关键数字；不要念代码、长路径或整段回复。"
        + "日常问答不要用，会打扰人。",
      parameters: {
        text: {
          type: "string",
          required: true,
          description: "要念出来的中文句子，口语化，不超过 80 字。",
        },
        sink: {
          type: "string",
          enum: ["local", "living"],
          description: "仅在你要强行指定出口时才传；一般别传，让策略判断。",
        },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: false,
          properties: { spoke: { type: "boolean", required: true } },
        },
        // 必须给 render：只给 schema 不给渲染器，工具回执会在渲染阶段抛
        // "output.render failed: userRender is not a function"，调用方看到的是错误。
        render: (_args, value) => [{ type: "text", text: value.spoke ? "已交给喇叭" : "没有发出" }],
      },
      async execute(args) {
        const text = String(args.text ?? "").trim();
        if (!text) throw new Error("speak 需要非空文本");
        if (text.length > 200) throw new Error("speak 一次最多 200 字，通知不该这么长");
        if (args.sink === "local" || args.sink === "living") {
          send(text, { sink: args.sink });   // 调用方明确指定，策略不插手
        } else {
          await speakByPolicy(text);
        }
        return { spoke: true };
      },
    }),
  );

  // ---------------------------------------------------------------- 被动：长任务落定
  if (!config.autoAnnounce) return;

  const minMs = Math.max(30, Number(config.minSeconds) || 300) * 1000;

  ctx.on("session/event", (session, event) => {
    if (event.type !== "job/settled") return;
    const d = event.data;
    const elapsedMs = Math.max(0, (d.finishedAt ?? 0) - (d.startedAt ?? 0));
    const failed = d.status !== "completed";
    // 失败值得更早知道，所以门槛放低；成功必须跑够 minSeconds 才响
    const worth = failed ? elapsedMs >= 60_000 : elapsedMs >= minMs;
    if (!worth) return;

    const label = cleanLabel(d.label || d.kind);
    const verdict = d.status === "completed" ? "完成" : d.status === "killed" ? "被中断" : "出错了";
    void speakByPolicy(`${label}${verdict}，耗时 ${fmtDuration(elapsedMs / 1000)}`);
  });
}
