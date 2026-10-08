// dsvc — 解码直通服务（一期 1a）
// 协议（v1）：
//   stdin  = JSONL 命令：
//     {"op":"load","src":"...","spans":[[start,end,rate],...],"fps":60,"mode":"frames"}
//     {"op":"seek","virtual_frame":K,"src":"...","spans":[...],"fps":60}
//     {"op":"stop"} {"op":"quit"}
//   stdout = 裸 I420 帧字节流（紧凑 w*h*1.5/帧，无帧头；帧边界由 w*h*1.5 定长切分）
//   stderr = JSONL 事件：{"ev":"gen","gen":N} / {"ev":"loaded","kfs":..,"gen":N} /
//            {"ev":"seek_ok","gen":N} / {"ev":"eof","pushed":N,"gen":N} / {"ev":"error",...}
// 段表语义：spans 升序不重叠，rate=业务倍速（1=原速；10=该段 10 帧取 1 帧重编号）
//           每段保留帧数 = ceil(floor((end-start)*fps) / rate)，相位锚定段首（src_idx%rate==0）
// 虚拟轴：推流顺序即 CFR fps 虚拟时间轴（第 N 帧 = N/fps 秒）
// 段边界两模式（1d 小间隙连续解码）：
//   restart（大间隙）：段尾发空包进 drain 模式吐尽余帧（按 pts 归属），再 flush_buffers
//     复位，下一段从其关键帧锚点重启。干净重建由恒等式自动保证：
//     restart ⟺ 间隙帧数 > 下段回填帧数+cap ⟺ anchor(next) > b(cur)+cap 帧
//     ⟹ 锚点关键帧包必然尚未流过（demux 位置 dts≈b），从锚点重喂即可位级重建。
//   cont（小间隙：间隙帧数 ≤ 下段锚点回填帧数+cap；cap 默认 16=流水线深，
//     可用环境变量 DSVC_CONT_GAP_FRAMES 覆盖，负值=禁用连续路径全走 restart）：
//     解码器零复位，参考链=真实源序列（位级比锚点重启更干净），间隙包照喂，
//     段外帧由 emit 的 pts 归属丢弃。连续跨段瞬间流水线内仍压有旧段余帧，
//     必须按帧自身 pts 归属（旧实现按喂包段号过滤会把它们丢掉）。
// 包馈送（1d 重构）：全部视频包按 demux（dts）序直喂，无扣留池——
//   v1 按 pts≥段尾扣留未来包，但 B 帧的未来参考 P 包恰是 pts≥段尾∧dts<段尾，
//   扣留使其 pts<段尾 的 B 依赖先喂=参考链断裂、段尾 ~3 帧微损坏（v1 潜伏缺陷，
//   probe_dsvc_cont 实测）。解码器按 pts 序出帧 + emit 按帧 pts 归属 ⇒ 直喂不乱账；
//   restart 链仍锚点跳过、间隙仅喂 [anchor,a) 回填段；连续链间隙包全喂。
//   帧归属一律按帧 pts 二分查段表（span_of）——解码器按 pts 序出帧，
//   故推流顺序仍=段序，pushed/span 事件/每段账目语义与 v1 兼容。
// span 事件：{"ev":"span","si":N,"pushed":M,"gen":G,"mode":"restart|cont"}（mode=1d 新增）
use ffmpeg_the_third as ffmpeg;
use ffmpeg_the_third::codec::threading;
use serde::Deserialize;
use std::collections::HashMap;
use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

fn tb_f64(r: ffmpeg::util::rational::Rational) -> f64 {
    r.0 as f64 / r.1 as f64
}

#[derive(Deserialize, Clone)]
struct StreamCtx {
    src: String,
    // [start, end, rate] 源秒；rate=1 原速
    spans: Vec<[f64; 3]>,
    #[serde(default = "def_fps")]
    fps: f64,
    // 帧包双输出接口：frames=解码写 stdout；packets=预留（W 路线，v1 拒绝）
    #[serde(default = "def_mode")]
    mode: String,
}
fn def_fps() -> f64 {
    60.0
}
fn def_mode() -> String {
    "frames".into()
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Cmd {
    Load(StreamCtx),
    Seek {
        virtual_frame: u64,
        #[serde(flatten)]
        ctx: StreamCtx,
    },
    Stop,
    Quit,
}

struct Shared {
    generation: AtomicU64,
    quit: AtomicBool,
    stop: AtomicBool,
    kf_cache: Mutex<HashMap<String, Vec<f64>>>, // src -> 关键帧秒列表（全片）
}

fn ev(line: &str) {
    let mut err = std::io::stderr().lock();
    let _ = writeln!(err, "{}", line);
    let _ = err.flush();
}

// seek 目标定位：虚拟帧 K → (span 索引, 段内源帧号)
fn locate(ctx: &StreamCtx, k: u64) -> Option<(usize, u64)> {
    let mut acc = 0u64;
    for (i, sp) in ctx.spans.iter().enumerate() {
        let n_src = ((sp[1] - sp[0]) * ctx.fps).floor() as u64;
        let rate = (sp[2].max(1.0)) as u64;
        let kept = n_src.div_ceil(rate);
        if k < acc + kept {
            let m = k - acc; // 段内第 m 个保留帧（0 基），源帧号 = m*rate
            return Some((i, m * rate));
        }
        acc += kept;
    }
    None
}

// 帧归属：按帧 pts 二分查段表（a<=t<b）；落在删除间隙/首段前/末段后 → None（丢弃）。
// 1d 起取代「喂包时的段号」归属：连续边界不冲洗 + B 帧重排使流水线内同时压有
// 跨段余帧，唯有按帧自身 pts 落段才能不丢不错。
fn span_of(spans: &[[f64; 3]], t: f64) -> Option<usize> {
    let idx = spans.partition_point(|sp| sp[0] <= t);
    if idx == 0 {
        return None;
    }
    let i = idx - 1;
    if t < spans[i][1] {
        Some(i)
    } else {
        None
    }
}

// 段 si 的锚点回填帧数：段首 a 到其锚点关键帧（≤a 的最近 kf）的距离。
// restart 时这段必须从锚点重解码（不产出，只为重建参考链）=换段固定成本主体。
fn backfill_frames(spans: &[[f64; 3]], kfs: &[f64], fps: f64, si: usize) -> f64 {
    let a = spans[si][0];
    let kf_i = kfs.partition_point(|&k| k <= a);
    let anchor = if kf_i > 0 { kfs[kf_i - 1] } else { kfs[0] };
    (a - anchor).max(0.0) * fps
}

// 小间隙连续边界判据：间隙帧数 ≤ 下段锚点回填帧数+cap（cap 默认 16=流水线深，
// DSVC_CONT_GAP_FRAMES 可覆盖，负值=禁用连续）。
// 恒等式（restart 正确性依据）：gap > backfill+cap ⟺ anchor(next) > b(cur)+cap/fps 秒，
// 即走 restart 时锚点关键帧必在删除间隙内且超前 demux 位置 ≥cap 帧。
fn is_continuous(spans: &[[f64; 3]], kfs: &[f64], fps: f64, si: usize, cap: f64) -> bool {
    if si + 1 >= spans.len() || cap < 0.0 {
        return false;
    }
    let gap = (spans[si + 1][0] - spans[si][1]).max(0.0) * fps;
    gap <= backfill_frames(spans, kfs, fps, si + 1) + cap
}

// 连续判据 cap（帧）：默认 16；DSVC_CONT_GAP_FRAMES 覆盖，钳位 [0,1000]——
// 负值/cap=0 语义=「仅零间隙(相邻段)连续」，即棒 §4.0m 预案的退路（收紧阈值）。
// 注意不可强制非法 restart：restart 合法性依赖恒等式 anchor>b（锚点包未流过），
// 若对 anchor≤b 的边界强行 restart，锚点无法重喂=参考链缺失=段坏。
fn cont_gap_cap_frames() -> f64 {
    match std::env::var("DSVC_CONT_GAP_FRAMES") {
        Ok(s) => s.trim().parse::<f64>().map(|v| v.clamp(0.0, 1000.0)).unwrap_or(16.0),
        Err(_) => 16.0,
    }
}

// 1e：非参考 B 帧按需丢弃开关（默认开；DSVC_DISCARD_NONREF=0 关闭——
// A/B 对照与退路）
fn discard_nonref_enabled() -> bool {
    match std::env::var("DSVC_DISCARD_NONREF") {
        Ok(s) => !s.trim().eq("0"),
        Err(_) => true,
    }
}

// h264 包首 slice NAL 的 nal_ref_idc（Some(0)=非参考帧；Some(>0)=参考帧；
// None=无法判定→调用方按参考处理，宁可不丢不可错丢）。
// 封装自适应：AnnexB（00 00 01 起始码）与 AVCC（4 字节大端长度前缀）。
// slice 类型 1..=5（非 IDR/IDR）；SPS(7)/PPS(8)/SEI(6)/AUD(9) 跳过。
// 一帧内多 slice 共享该帧参考属性，首 slice 即判。
fn h264_first_slice_ref_idc(data: &[u8]) -> Option<u8> {
    let n = data.len();
    if n < 5 {
        return None;
    }
    let annexb = (data[0] == 0 && data[1] == 0 && data[2] == 1)
        || (data[0] == 0 && data[1] == 0 && data[2] == 0 && data[3] == 1);
    let mut i = 0usize;
    let mut guard = 0;
    while i + 1 < n && guard < 128 {
        guard += 1;
        if annexb {
            // 找下一起始码 00 00 01
            let mut j = i;
            let mut start = None;
            while j + 2 < n {
                if data[j] == 0 && data[j + 1] == 0 && data[j + 2] == 1 {
                    start = Some(j + 3);
                    break;
                }
                j += 1;
            }
            let s = start?;
            let t = data[s] & 0x1f;
            if (1..=5).contains(&t) {
                return Some(data[s] >> 5);
            }
            i = s + 1;
        } else {
            // AVCC：4 字节大端长度
            if i + 4 > n {
                return None;
            }
            let l = u32::from_be_bytes([data[i], data[i + 1], data[i + 2], data[i + 3]])
                as usize;
            i += 4;
            if l == 0 || i + l > n {
                return None;
            }
            let t = data[i] & 0x1f;
            if (1..=5).contains(&t) {
                return Some(data[i] >> 5);
            }
            i += l;
        }
    }
    None
}

// 段尾参考窗（帧）：restart 边界锚点前的间隙包、以及末段之后的包，凡 pts 落在
// 段尾后此窗内者照喂——它们是段尾 B 帧的未来参考（ctts 深度 ≤ 数帧），扣掉会
// 段尾微损坏；喂入后其帧被 pts 归属丢弃，IDR 锚点自动复位参考链，无害。
const TAIL_REF_WINDOW_FRAMES: f64 = 16.0;

// 紧凑平面拷贝：yuv420p(可能带 stride padding) → I420 紧凑 w*h*1.5
fn frame_to_i420(fr: &ffmpeg::util::frame::Video, w: usize, h: usize) -> Option<Vec<u8>> {
    if fr.format() != ffmpeg::util::format::pixel::Pixel::YUV420P {
        return None;
    }
    let (ls0, ls1) = (fr.stride(0), fr.stride(1));
    let (cw, ch) = (w / 2, h / 2);
    let mut out = Vec::with_capacity(w * h * 3 / 2);
    let y = fr.data(0);
    let u = fr.data(1);
    let v = fr.data(2);
    if ls0 == w && ls1 == cw {
        out.extend_from_slice(&y[..w * h]);
        out.extend_from_slice(&u[..cw * ch]);
        out.extend_from_slice(&v[..cw * ch]);
    } else {
        for row in 0..h {
            out.extend_from_slice(&y[row * ls0..row * ls0 + w]);
        }
        for row in 0..ch {
            out.extend_from_slice(&u[row * ls1..row * ls1 + cw]);
        }
        for row in 0..ch {
            out.extend_from_slice(&v[row * ls1..row * ls1 + cw]);
        }
    }
    Some(out)
}

fn is_eagain(e: &ffmpeg::Error) -> bool {
    matches!(e, ffmpeg::Error::Other { errno: 11 }) // libc EAGAIN
}

fn main() {
    if ffmpeg::init().is_err() {
        ev(r#"{"ev":"error","msg":"ffmpeg init failed"}"#);
        std::process::exit(2);
    }
    let shared = Arc::new(Shared {
        generation: AtomicU64::new(0),
        quit: AtomicBool::new(false),
        stop: AtomicBool::new(false),
        kf_cache: Mutex::new(HashMap::new()),
    });
    let (tx, rx) = std::sync::mpsc::channel::<Cmd>();
    let shared2 = shared.clone();
    let writer = std::thread::spawn(move || decode_loop(&shared2, rx));

    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let cmd: Cmd = match serde_json::from_str(&line) {
            Ok(c) => c,
            Err(e) => {
                ev(&format!(r#"{{"ev":"error","msg":"bad cmd: {}"}}"#, e));
                continue;
            }
        };
        match &cmd {
            Cmd::Quit => {
                shared.quit.store(true, Ordering::SeqCst);
                shared.stop.store(true, Ordering::SeqCst);
                let _ = tx.send(cmd);
                break;
            }
            Cmd::Stop => {
                shared.stop.store(true, Ordering::SeqCst);
                let _ = tx.send(cmd);
            }
            Cmd::Load(_) | Cmd::Seek { .. } => {
                shared.generation.fetch_add(1, Ordering::SeqCst);
                shared.stop.store(false, Ordering::SeqCst);
                let _ = tx.send(cmd);
            }
        }
    }
    shared.quit.store(true, Ordering::SeqCst);
    shared.stop.store(true, Ordering::SeqCst);
    let _ = tx.send(Cmd::Quit); // stdin EOF（非显式 quit）时唤醒 decode_loop，防 join 死锁
    let _ = writer.join();
}

fn decode_loop(shared: &Arc<Shared>, rx: std::sync::mpsc::Receiver<Cmd>) {
    let mut stdout = std::io::stdout().lock();
    loop {
        let cmd = match rx.recv() {
            Ok(c) => c,
            Err(_) => return,
        };
        let (ctx, target) = match cmd {
            Cmd::Quit => return,
            Cmd::Stop => continue,
            Cmd::Load(ctx) => (ctx, None),
            Cmd::Seek { virtual_frame, ctx } => (ctx, Some(virtual_frame)),
        };
        let gen = shared.generation.load(Ordering::SeqCst);
        ev(&format!(r#"{{"ev":"gen","gen":{}}}"#, gen));
        if let Err(e) = run_stream(shared, &mut stdout, &ctx, gen, target) {
            ev(&format!(
                r#"{{"ev":"error","msg":"{}","gen":{}}}"#,
                e.replace('"', "'"),
                gen
            ));
        }
    }
}

fn kf_index(
    shared: &Arc<Shared>,
    src: &str,
    vidx: usize,
    tb: f64,
) -> Result<Vec<f64>, String> {
    if let Some(kfs) = shared.kf_cache.lock().unwrap().get(src) {
        return Ok(kfs.clone());
    }
    let mut ictx = ffmpeg::format::input(src).map_err(|e| format!("open: {}", e))?;
    let mut kfs: Vec<f64> = Vec::new();
    for r in ictx.packets() {
        let (_s, pkt) = r.map_err(|e| format!("demux: {}", e))?;
        if pkt.stream() != vidx {
            continue;
        }
        let pts = pkt.pts().map(|p| p as f64 * tb).unwrap_or(-1.0);
        if pts < 0.0 {
            continue;
        }
        if pkt.is_key() {
            kfs.push(pts);
        }
    }
    shared.kf_cache.lock().unwrap().insert(src.to_string(), kfs.clone());
    Ok(kfs)
}

// 帧产出上下文：过滤规则（段表/锚点相位/seek 目标）+ 写出
struct Emit<'a> {
    spans: &'a [[f64; 3]],
    kfs: &'a [f64],
    fps: f64,
    cont_cap: f64,
    tb: f64,
    w: usize,
    h: usize,
    target: Option<u64>,
    target_si: usize,
    target_src_idx: u64,
    gen: u64,
    shared: &'a Arc<Shared>,
    stdout: &'a mut dyn Write,
    pushed: &'a mut u64,
    seek_ok_sent: &'a mut bool,
    per_span: &'a mut Vec<u64>,
    last_span_emitted: &'a mut Option<usize>,
}

// 段 si 的进入方式（纯函数，与推段 drain 决策同源，无状态无竞态）：
// restart=锚点重启（含首段）；cont=小间隙连续（解码器未复位）
fn entry_is_restart(spans: &[[f64; 3]], kfs: &[f64], fps: f64, si: usize, cap: f64) -> bool {
    si == 0 || !is_continuous(spans, kfs, fps, si - 1, cap)
}

// 返回 Ok(false)=停止整条流（下游关闭/换代）
fn emit_one(c: &mut Emit, frame: &ffmpeg::util::frame::Video) -> Result<bool, String> {
    if c.shared.generation.load(Ordering::SeqCst) != c.gen {
        return Ok(false);
    }
    let fpts = frame.pts().map(|p| p as f64 * c.tb).unwrap_or(-1.0);
    // pts 归属：连续边界下流水线内压有跨段余帧（前段迟到帧），按帧自身 pts 落段；
    // 间隙帧=None 丢弃。解码器 pts 序出帧 ⇒ 推流顺序仍=段序。
    let span_i = match span_of(c.spans, fpts) {
        Some(i) => i,
        None => return Ok(true),
    };
    if c.target.is_some() && span_i < c.target_si {
        return Ok(true); // seek：目标段之前的余帧（含前段迟到帧）不产出
    }
    let (a, rate) = (c.spans[span_i][0], (c.spans[span_i][2].max(1.0)) as u64);
    let src_idx = ((fpts - a) * c.fps).round() as i64;
    if src_idx < 0 || (src_idx as u64) % rate != 0 {
        return Ok(true); // 抽帧相位锚定段首
    }
    let sidx = src_idx as u64;
    if c.target.is_some() && span_i == c.target_si && sidx < c.target_src_idx {
        return Ok(true); // seek：目标源帧之前丢弃
    }
    let bytes = match frame_to_i420(frame, c.w, c.h) {
        Some(b) => b,
        None => return Err("frame format not yuv420p".into()),
    };
    if c.stdout.write_all(&bytes).is_err() {
        return Ok(false); // 下游关闭
    }
    *c.pushed += 1;
    while c.per_span.len() <= span_i {
        c.per_span.push(0);
    }
    c.per_span[span_i] += 1;
    if *c.last_span_emitted != Some(span_i) {
        *c.last_span_emitted = Some(span_i);
        // 段首事件：pushed=含本帧的累计计数（本帧虚拟索引=pushed-1）；mode=进入方式
        let mode = if entry_is_restart(c.spans, c.kfs, c.fps, span_i, c.cont_cap) {
            "restart"
        } else {
            "cont"
        };
        ev(&format!(
            r#"{{"ev":"span","si":{},"pushed":{},"gen":{},"mode":"{}"}}"#,
            span_i, *c.pushed, c.gen, mode
        ));
    }
    if !*c.seek_ok_sent && c.target.is_some() && span_i == c.target_si && sidx >= c.target_src_idx {
        *c.seek_ok_sent = true;
        ev(&format!(r#"{{"ev":"seek_ok","gen":{}}}"#, c.gen));
    }
    Ok(true)
}

// 解码泵：收帧优先；receive EAGAIN 才发当前包；包永不丢弃
fn pump_packet(
    decoder: &mut ffmpeg::codec::decoder::Video,
    pkt: &ffmpeg::Packet,
    fr: &mut ffmpeg::util::frame::Video,
    emit: &mut dyn FnMut(&ffmpeg::util::frame::Video) -> Result<bool, String>,
) -> Result<bool, String> {
    let mut sent = false;
    loop {
        match decoder.receive_frame(fr) {
            Ok(()) => {
                if !emit(fr)? {
                    return Ok(false);
                }
            }
            Err(e) => {
                if is_eagain(&e) {
                    if sent {
                        return Ok(true);
                    }
                    match decoder.send_packet(pkt) {
                        Ok(()) => sent = true,
                        Err(e2) if is_eagain(&e2) => continue,
                        Err(_) => return Ok(true),
                    }
                } else {
                    return Ok(true); // EOF 或其他错误
                }
            }
        }
    }
}

// 段尾 drain：空包进 drain 模式吐尽流水线余帧，再复位解码器
fn drain_span(
    decoder: &mut ffmpeg::codec::decoder::Video,
    fr: &mut ffmpeg::util::frame::Video,
    emit: &mut dyn FnMut(&ffmpeg::util::frame::Video) -> Result<bool, String>,
) -> Result<bool, String> {
    let mut sent = false;
    loop {
        if !sent {
            match decoder.send_packet(&ffmpeg::Packet::empty()) {
                Ok(()) => sent = true,
                Err(e) if is_eagain(&e) => std::thread::yield_now(), // 需先收帧腾位
                Err(_) => {
                    decoder.flush();
                    return Ok(true);
                }
            }
        }
        match decoder.receive_frame(fr) {
            Ok(()) => {
                if !emit(fr)? {
                    decoder.flush();
                    return Ok(false);
                }
            }
            Err(e) => {
                if is_eagain(&e) {
                    std::thread::yield_now();
                    continue;
                }
                break; // EOF=drain 完成
            }
        }
    }
    decoder.flush(); // 复位；下一段从关键帧锚点重启
    Ok(true)
}

fn run_stream(
    shared: &Arc<Shared>,
    stdout: &mut dyn std::io::Write,
    ctx: &StreamCtx,
    gen: u64,
    target: Option<u64>,
) -> Result<(), String> {
    if ctx.mode != "frames" {
        return Err(format!("mode {} not implemented in v1", ctx.mode));
    }
    let spans = &ctx.spans;
    if spans.is_empty() {
        return Err("empty spans".into());
    }
    let (target_si, target_src_idx) = match target {
        Some(k) => locate(ctx, k).ok_or("seek target beyond virtual axis")?,
        None => (0, 0),
    };

    let mut ictx = ffmpeg::format::input(&ctx.src).map_err(|e| format!("open: {}", e))?;
    let stream = ictx
        .streams()
        .best(ffmpeg::media::Type::Video)
        .ok_or("no video stream")?;
    let vidx = stream.index();
    let tb = tb_f64(stream.time_base());
    let kfs = kf_index(shared, &ctx.src, vidx, tb)?;
    if kfs.is_empty() {
        return Err("no keyframes".into());
    }
    drop(ictx);
    if target.is_none() {
        ev(&format!(r#"{{"ev":"loaded","kfs":{},"gen":{}}}"#, kfs.len(), gen));
    }

    let mut ictx = ffmpeg::format::input(&ctx.src).map_err(|e| format!("open2: {}", e))?;
    let stream = ictx.streams().best(ffmpeg::media::Type::Video).unwrap();
    let mut cctx =
        ffmpeg::codec::context::Context::from_parameters(stream.parameters()).map_err(|e| e.to_string())?;
    cctx.set_threading(threading::Config {
        kind: threading::Type::Frame,
        count: 0,
    });
    let mut decoder = cctx.decoder().video().map_err(|e| format!("decoder: {}", e))?;
    let (w, h) = (decoder.width() as usize, decoder.height() as usize);

    let mut pushed: u64 = 0;
    let mut nonref_skipped: u64 = 0;
    let mut seek_ok_sent = false;
    let mut per_span: Vec<u64> = Vec::new();
    let mut last_span_emitted: Option<usize> = None;
    let mut fr = ffmpeg::util::frame::Video::empty();
    let cont_cap = cont_gap_cap_frames();
    let mut emit = Emit {
        spans,
        kfs: &kfs,
        fps: ctx.fps,
        cont_cap,
        tb,
        w,
        h,
        target,
        target_si,
        target_src_idx,
        gen,
        shared,
        stdout: &mut *stdout, // 借用而非移动：函数尾部仍可 flush
        pushed: &mut pushed,
        seek_ok_sent: &mut seek_ok_sent,
        per_span: &mut per_span,
        last_span_emitted: &mut last_span_emitted,
    };
    // emit 包装：帧归属已并入 emit_one（按 pts 查段表）
    let mut emit_fn = |frame: &ffmpeg::util::frame::Video| -> Result<bool, String> {
        emit_one(&mut emit, frame)
    };

    let mut si = 0usize;
    let mut decoder_live = false; // 解码器含未冲水状态；连续边界跨段时保持 true
    let discard_on = discard_nonref_enabled();
    // 包循环（1d 重构：全 dts 序直喂，无扣留池）：
    //   v1 按 pts≥段尾扣留「未来包」——但 B 帧的未来参考 P 包恰是 pts≥段尾∧dts<段尾，
    //   扣留后其 pts<段尾 的 B 依赖先喂=参考链断裂，段尾 ~3 帧微损坏（v1 潜伏缺陷，
    //   probe_dsvc_cont 相 B 实测段尾 units 57-59）。解码器按 pts 序出帧 + emit 按
    //   帧 pts 归属 ⇒ 直喂不乱账；restart 链的锚点跳过与间隙回填段照旧，恒等式保证
    //   锚点包未流过（restart ⟺ anchor(next) > b(cur)+cap 帧）。
    for r in ictx.packets() {
        if shared.quit.load(Ordering::SeqCst) || shared.generation.load(Ordering::SeqCst) != gen {
            return Ok(());
        }
        let (_s, pkt) = r.map_err(|e| format!("demux2: {}", e))?;
        if pkt.stream() != vidx {
            continue;
        }
        let pts = pkt.pts().map(|p| p as f64 * tb).unwrap_or(-1.0);
        if pts < 0.0 {
            continue;
        }
        let dts = pkt.dts().map(|p| p as f64 * tb).unwrap_or(pts);
        // 1) dts 推段：只为 restart 边界的 drain 时机（连续边界零动作零复位）
        while si < spans.len() && dts >= spans[si][1] {
            if decoder_live && !is_continuous(spans, &kfs, ctx.fps, si, cont_cap) {
                if !drain_span(&mut decoder, &mut fr, &mut emit_fn)? {
                    return Ok(());
                }
                decoder_live = false;
            }
            si += 1;
        }
        if si >= spans.len() {
            continue; // 末段之后（不解码）
        }
        if si < target_si {
            continue; // seek：目标段之前不解码（demux 快速跳过）
        }
        // 2) 归属与喂包决策（全 pts 驱动）
        let idx = spans.partition_point(|sp| sp[0] <= pts);
        let tail_window = TAIL_REF_WINDOW_FRAMES / ctx.fps;
        let mut will_display = false;
        // seek 落点：目标段（=target_si）在此代流中**必须按 restart 处理**——
        // 解码器全新（本代由 seek 建立），只能从关键帧喂起；且锚点前移到
        // 「目标帧之前最近的关键帧」（09-16 真机 T0）：沿用段首锚点会让长段
        // 深定位从段首回填（实测 600s 段 → 冻结 2.4s+，红条假死）。前移后
        // 回填 ≤1 个 GOP(≈120 帧≈0.2s)。正确性无损：锚点与目标之间的帧本
        // 就被 target 过滤丢弃（sidx < target_src_idx）。
        let seek_anchor: Option<f64> = if target.is_some() {
            let tgt_s = spans[target_si][0]
                + (target_src_idx as f64) / ctx.fps;
            let kf_i = kfs.partition_point(|&k| k <= tgt_s);
            Some(if kf_i > 0 { kfs[kf_i - 1] } else { kfs[0] })
        } else {
            None
        };
        let is_target_span = target.is_some() && idx > 0 && idx - 1 == target_si;
        let feed = if idx > 0 && pts < spans[idx - 1][1] {
            // 段内包：目标段用前移锚点强制 restart；其余段按逐边界自适应
            let strict = if is_target_span {
                true
            } else {
                entry_is_restart(spans, &kfs, ctx.fps, idx - 1, cont_cap)
            };
            if strict {
                let span_anchor = {
                    let kf_i = kfs.partition_point(|&k| k <= spans[idx - 1][0]);
                    if kf_i > 0 { kfs[kf_i - 1] } else { kfs[0] }
                };
                let anchor = match seek_anchor {
                    Some(a) if is_target_span => a.max(span_anchor),
                    _ => span_anchor,
                };
                pts >= anchor
            } else {
                true
            }
        } else if idx > 0 && idx < spans.len() {
            // 间隙包 [b_{idx-1}, a_idx)：连续链照喂（参考连续性必需）；restart 链喂
            // 两类——锚点回填段 [anchor(idx), a_idx)（重建参考链必需）+ 段尾参考窗
            // [b_{idx-1}, b_{idx-1}+窗)（段尾 B 帧的未来参考；两者之间跳过不解码）
            if is_continuous(spans, &kfs, ctx.fps, idx - 1, cont_cap) {
                true
            } else {
                let kf_i = kfs.partition_point(|&k| k <= spans[idx][0]);
                let anchor = if kf_i > 0 { kfs[kf_i - 1] } else { kfs[0] };
                pts >= anchor || pts < spans[idx - 1][1] + tail_window
            }
        } else if idx == spans.len() {
            // 末段之后 [b_last, …)：仅喂段尾参考窗内包（末段段尾 B 帧的未来参考）
            pts < spans[spans.len() - 1][1] + tail_window
        } else {
            false // 首段前
        };
        // 显示判定（与 emit_one 同式；emit 仍是权威过滤，此处仅供丢弃决策）
        if feed && idx > 0 && pts < spans[idx - 1][1] {
            let si = idx - 1;
            let pre_target = target.is_some() && si < target_si;
            if !pre_target {
                let a = spans[si][0];
                let rate = (spans[si][2].max(1.0)) as u64;
                let sidx = ((pts - a) * ctx.fps).round();
                if sidx >= 0.0
                    && pts < spans[si][1]
                    && (sidx as u64) % rate == 0
                    && !(target.is_some()
                        && si == target_si
                        && (sidx as u64) < target_src_idx)
                {
                    will_display = true;
                }
            }
        }
        if !feed {
            continue;
        }
        // 1e 非参考 B 帧按需丢弃：不显示且首 slice nal_ref_idc==0 的包，把解码器
        // skip_frame 拨到 NONREF——解码器跳过该帧解码且 DPB 一致性由其维护
        // （非参考帧本就不占 DPB）。参考帧按定义 idc>0 永不被丢 → 显示帧位级
        // mad=0 不变。帧级线程在包提交时快照 skip_frame（update_context_from_user），
        // 逐包切换无竞态。
        if discard_on {
            use ffmpeg::ffi::AVDiscard;
            let want = if !will_display
                && h264_first_slice_ref_idc(pkt.data().unwrap_or(&[])) == Some(0)
            {
                nonref_skipped += 1;
                AVDiscard::AVDISCARD_NONREF
            } else {
                AVDiscard::AVDISCARD_DEFAULT
            };
            unsafe {
                if (*decoder.as_mut_ptr()).skip_frame != want {
                    (*decoder.as_mut_ptr()).skip_frame = want;
                }
            }
        }
        decoder_live = true;
        if !pump_packet(&mut decoder, &pkt, &mut fr, &mut emit_fn)? {
            return Ok(());
        }
    }
    // 收尾：流水线余帧（按 pts 归属；含末段尾帧）
    if decoder_live {
        let _ = drain_span(&mut decoder, &mut fr, &mut emit_fn)?;
    }
    let _ = stdout.flush();
    let per_span_json = serde_json::to_string(&per_span).unwrap_or_else(|_| "[]".into());
    ev(&format!(
        r#"{{"ev":"eof","pushed":{},"gen":{},"per_span":{},"nonref_skipped":{}}}"#,
        pushed, gen, per_span_json, nonref_skipped
    ));
    Ok(())
}
