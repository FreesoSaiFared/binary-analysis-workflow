use rvfabric_sim::{Command, Response, WorkerConfig, WorkerState};
use std::env;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::thread;
use std::time::Duration;

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

fn handle_connection(stream: TcpStream, state: &mut WorkerState, cfg: &WorkerConfig) -> std::io::Result<()> {
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
                send(&mut writer, &response)?;
            }
            Command::Phase { generation, interval_id, job_id, phase_u8 } => {
                if let Err(reason) = state.validate_phase(generation, interval_id, job_id) {
                    send(&mut writer, &Response::Rejected { reason: reason.into() })?;
                    continue;
                }
                state.completed_jobs += 1;
                if cfg.drop_every > 0 && state.completed_jobs % cfg.drop_every == 0 {
                    let _ = stream.shutdown(Shutdown::Both);
                    break;
                }
                let throttle = if cfg.throttle_after_jobs > 0 && state.completed_jobs >= cfg.throttle_after_jobs {
                    cfg.throttle_multiplier.max(1.0)
                } else { 1.0 };
                let mut compute_ms = (cfg.npu_ms + cfg.rga_ms + cfg.vpu_ms) * throttle;
                compute_ms += deterministic_jitter(job_id, cfg.jitter_ms);
                if cfg.stall_every > 0 && state.completed_jobs % cfg.stall_every == 0 {
                    compute_ms += cfg.stall_ms;
                }
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
            Command::Reset { generation } => {
                let response = state.reset(generation);
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
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                if let Err(e) = handle_connection(stream, &mut state, &cfg) {
                    eprintln!("RVWORKER_SIM_CONNECTION_ERROR {e}");
                }
            }
            Err(e) => eprintln!("RVWORKER_SIM_ACCEPT_ERROR {e}"),
        }
    }
    Ok(())
}
