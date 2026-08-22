use rvfabric_sim::{interpolate_rgb_u8, Command, Response, WorkerConfig, WorkerState};
use std::env;
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::thread;
use std::time::Duration;

#[derive(Debug, Clone)]
struct PixelCache {
    generation: u64,
    interval_id: u64,
    width: usize,
    height: usize,
    left: Vec<u8>,
    right: Vec<u8>,
}

fn arg<T: std::str::FromStr>(name: &str, default: T) -> T {
    let args: Vec<String> = env::args().collect();
    args.windows(2)
        .find(|w| w[0] == name)
        .and_then(|w| w[1].parse::<T>().ok())
        .unwrap_or(default)
}

fn send(writer: &mut BufWriter<TcpStream>, response: &Response) -> std::io::Result<()> {
    serde_json::to_writer(&mut *writer, response)?;
    writer.write_all(b"\n")?;
    writer.flush()
}

fn deterministic_jitter(job_id: u64, max_ms: f64) -> f64 {
    if max_ms <= 0.0 { return 0.0; }
    let x = job_id.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
    ((x >> 33) as f64 / (u32::MAX as f64)) * max_ms
}

fn begin_job(state: &mut WorkerState, cfg: &WorkerConfig, job_id: u64) -> Option<f64> {
    state.completed_jobs += 1;
    if cfg.drop_every > 0 && state.completed_jobs % cfg.drop_every == 0 {
        return None;
    }
    let throttle = if cfg.throttle_after_jobs > 0 && state.completed_jobs >= cfg.throttle_after_jobs {
        cfg.throttle_multiplier.max(1.0)
    } else { 1.0 };
    let mut compute_ms = (cfg.npu_ms + cfg.rga_ms + cfg.vpu_ms) * throttle;
    compute_ms += deterministic_jitter(job_id, cfg.jitter_ms);
    if cfg.stall_every > 0 && state.completed_jobs % cfg.stall_every == 0 {
        compute_ms += cfg.stall_ms;
    }
    Some(compute_ms)
}

fn handle_connection(
    stream: TcpStream,
    state: &mut WorkerState,
    pixel_cache: &mut Option<PixelCache>,
    cfg: &WorkerConfig,
) -> std::io::Result<()> {
    stream.set_nodelay(true)?;
    let reader_stream = stream.try_clone()?;
    let mut reader = BufReader::new(reader_stream);
    let mut writer = BufWriter::new(stream.try_clone()?);
    let mut line = String::new();

    loop {
        line.clear();
        if reader.read_line(&mut line)? == 0 { break; }
        let command: Command = match serde_json::from_str(line.trim_end()) {
            Ok(v) => v,
            Err(e) => {
                send(&mut writer, &Response::Rejected { reason: format!("bad_command:{e}") })?;
                continue;
            }
        };
        match command {
            Command::Hello { protocol } => {
                if protocol != "RVFABRIC/1" {
                    send(&mut writer, &Response::Rejected { reason: "protocol_mismatch".into() })?;
                } else {
                    send(&mut writer, &Response::HelloAck { protocol })?;
                }
            }
            Command::CacheInterval { generation, interval_id, left_frame_id, right_frame_id, .. } => {
                let response = state.handle_cache(generation, interval_id, left_frame_id, right_frame_id);
                if matches!(response, Response::Cached { .. }) {
                    *pixel_cache = None;
                }
                send(&mut writer, &response)?;
            }
            Command::CacheIntervalPixels {
                generation,
                interval_id,
                left_frame_id,
                right_frame_id,
                width,
                height,
                left_bytes,
                right_bytes,
            } => {
                if let Err(reason) = state.validate_cache_generation(generation) {
                    send(&mut writer, &Response::Rejected { reason: reason.into() })?;
                    continue;
                }
                let expected = width.checked_mul(height).and_then(|x| x.checked_mul(3));
                if expected != Some(left_bytes) || expected != Some(right_bytes) || left_bytes == 0 {
                    send(&mut writer, &Response::Rejected { reason: "pixel_shape_or_size_mismatch".into() })?;
                    continue;
                }
                let total = left_bytes + right_bytes;
                send(&mut writer, &Response::PayloadReady { generation, interval_id, total_bytes: total })?;
                let mut payload = vec![0u8; total];
                reader.read_exact(&mut payload)?;
                let right = payload.split_off(left_bytes);
                let left = payload;
                let response = state.handle_cache(generation, interval_id, left_frame_id, right_frame_id);
                if matches!(response, Response::Cached { .. }) {
                    *pixel_cache = Some(PixelCache { generation, interval_id, width, height, left, right });
                }
                send(&mut writer, &response)?;
            }
            Command::Phase { generation, interval_id, job_id, phase_u8 } => {
                if let Err(reason) = state.validate_phase(generation, interval_id, job_id) {
                    send(&mut writer, &Response::Rejected { reason: reason.into() })?;
                    continue;
                }
                let Some(compute_ms) = begin_job(state, cfg, job_id) else {
                    let _ = stream.shutdown(Shutdown::Both);
                    break;
                };
                thread::sleep(Duration::from_secs_f64(compute_ms / 1000.0));
                send(&mut writer, &Response::Result {
                    generation,
                    interval_id,
                    job_id,
                    phase_u8,
                    simulated_compute_ms: compute_ms,
                    encoded_bytes: cfg.encoded_bytes,
                })?;
            }
            Command::PhasePixels { generation, interval_id, job_id, phase_u8 } => {
                let cache_ok = pixel_cache.as_ref().map(|c|
                    c.generation == generation && c.interval_id == interval_id && c.left.len() == c.width * c.height * 3
                ).unwrap_or(false);
                if !cache_ok {
                    send(&mut writer, &Response::Rejected { reason: "pixel_cache_miss".into() })?;
                    continue;
                }
                if let Err(reason) = state.validate_phase(generation, interval_id, job_id) {
                    send(&mut writer, &Response::Rejected { reason: reason.into() })?;
                    continue;
                }
                let Some(compute_ms) = begin_job(state, cfg, job_id) else {
                    let _ = stream.shutdown(Shutdown::Both);
                    break;
                };
                thread::sleep(Duration::from_secs_f64(compute_ms / 1000.0));
                let cache = pixel_cache.as_ref().unwrap();
                let output = interpolate_rgb_u8(&cache.left, &cache.right, phase_u8);
                send(&mut writer, &Response::ResultPixels {
                    generation,
                    interval_id,
                    job_id,
                    phase_u8,
                    simulated_compute_ms: compute_ms,
                    payload_bytes: output.len(),
                })?;
                writer.write_all(&output)?;
                writer.flush()?;
            }
            Command::Reset { generation } => {
                let response = state.reset(generation);
                if matches!(response, Response::ResetAck { .. }) {
                    *pixel_cache = None;
                }
                send(&mut writer, &response)?;
            }
        }
    }
    Ok(())
}

fn main() -> std::io::Result<()> {
    let port: u16 = arg("--port", 19110u16);
    let cfg = WorkerConfig {
        npu_ms: arg("--npu-ms", 20.0),
        rga_ms: arg("--rga-ms", 2.0),
        vpu_ms: arg("--vpu-ms", 1.0),
        jitter_ms: arg("--jitter-ms", 0.0),
        encoded_bytes: arg("--encoded-bytes", 20_000usize),
        drop_every: arg("--drop-every", 0u64),
        stall_every: arg("--stall-every", 0u64),
        stall_ms: arg("--stall-ms", 0.0),
        throttle_after_jobs: arg("--throttle-after-jobs", 0u64),
        throttle_multiplier: arg("--throttle-multiplier", 1.0),
    };
    let listener = TcpListener::bind(("127.0.0.1", port))?;
    eprintln!("RVWORKER_SIM_READY port={port} cfg={}", serde_json::to_string(&cfg).unwrap());
    let mut state = WorkerState::default();
    let mut pixel_cache: Option<PixelCache> = None;
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                if let Err(e) = handle_connection(stream, &mut state, &mut pixel_cache, &cfg) {
                    eprintln!("RVWORKER_SIM_CONNECTION_ERROR {e}");
                }
            }
            Err(e) => eprintln!("RVWORKER_SIM_ACCEPT_ERROR {e}"),
        }
    }
    Ok(())
}
