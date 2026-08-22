use rvfabric_sim::{interpolate_rgb_u8, phase_positions, Command, Response};
use serde::{Deserialize, Serialize};
use std::env;
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::net::{TcpListener, TcpStream, ToSocketAddrs};
use std::time::Duration;

#[derive(Debug, Deserialize)]
struct Request {
    protocol: String,
    generation: u64,
    source_fps: u32,
    target_fps: u32,
    width: usize,
    height: usize,
    frame_count: usize,
}

#[derive(Debug, Serialize)]
struct Reply {
    protocol: &'static str,
    status: &'static str,
    generation: u64,
    width: usize,
    height: usize,
    output_frames: usize,
    payload_bytes: usize,
    worker_generated_count: usize,
    fallback_count: usize,
    failures: Vec<String>,
    claim_scope: &'static str,
}

fn arg_string(name: &str, default: &str) -> String {
    let a: Vec<String> = env::args().collect();
    a.windows(2).find(|w| w[0] == name).map(|w| w[1].clone()).unwrap_or_else(|| default.into())
}

fn arg<T: std::str::FromStr>(name: &str, default: T) -> T {
    let a: Vec<String> = env::args().collect();
    a.windows(2).find(|w| w[0] == name).and_then(|w| w[1].parse().ok()).unwrap_or(default)
}

fn send_json<T: Serialize>(w: &mut BufWriter<TcpStream>, value: &T) -> Result<(), String> {
    serde_json::to_writer(&mut *w, value).map_err(|e| e.to_string())?;
    w.write_all(b"\n").map_err(|e| e.to_string())?;
    w.flush().map_err(|e| e.to_string())
}

fn read_response(r: &mut BufReader<TcpStream>) -> Result<Response, String> {
    let mut line = String::new();
    if r.read_line(&mut line).map_err(|e| e.to_string())? == 0 { return Err("eof".into()); }
    serde_json::from_str(line.trim_end()).map_err(|e| e.to_string())
}

fn connect(addr: &str, timeout: Duration) -> Result<(BufReader<TcpStream>, BufWriter<TcpStream>), String> {
    let socket = addr.to_socket_addrs().map_err(|e| e.to_string())?.next().ok_or("no_address")?;
    let stream = TcpStream::connect_timeout(&socket, timeout).map_err(|e| e.to_string())?;
    stream.set_nodelay(true).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(timeout)).map_err(|e| e.to_string())?;
    stream.set_write_timeout(Some(timeout)).map_err(|e| e.to_string())?;
    let mut w = BufWriter::new(stream.try_clone().map_err(|e| e.to_string())?);
    let mut r = BufReader::new(stream);
    send_json(&mut w, &Command::Hello { protocol: "RVFABRIC/1".into() })?;
    match read_response(&mut r)? {
        Response::HelloAck { .. } => Ok((r, w)),
        x => Err(format!("hello:{x:?}")),
    }
}

fn cache_pair(
    r: &mut BufReader<TcpStream>, w: &mut BufWriter<TcpStream>, generation: u64,
    interval: u64, width: usize, height: usize, left: &[u8], right: &[u8],
) -> Result<(), String> {
    send_json(w, &Command::CacheIntervalPixels {
        generation, interval_id: interval, left_frame_id: interval, right_frame_id: interval + 1,
        width, height, left_bytes: left.len(), right_bytes: right.len(),
    })?;
    match read_response(r)? {
        Response::PayloadReady { total_bytes, .. } if total_bytes == left.len() + right.len() => {}
        Response::Rejected { reason } => return Err(reason),
        x => return Err(format!("payload_ready:{x:?}")),
    }
    w.write_all(left).map_err(|e| e.to_string())?;
    w.write_all(right).map_err(|e| e.to_string())?;
    w.flush().map_err(|e| e.to_string())?;
    match read_response(r)? {
        Response::Cached { generation: g, interval_id: i } if g == generation && i == interval => Ok(()),
        x => Err(format!("cache_commit:{x:?}")),
    }
}

fn phase(
    r: &mut BufReader<TcpStream>, w: &mut BufWriter<TcpStream>, generation: u64,
    interval: u64, position: usize, phase_u8: u8, expected: usize,
) -> Result<Vec<u8>, String> {
    let job_id = interval * 1_000_000 + position as u64;
    send_json(w, &Command::PhasePixels { generation, interval_id: interval, job_id, phase_u8 })?;
    match read_response(r)? {
        Response::ResultPixels { payload_bytes, job_id: j, .. } if payload_bytes == expected && j == job_id => {
            let mut pixels = vec![0u8; expected];
            r.read_exact(&mut pixels).map_err(|e| e.to_string())?;
            Ok(pixels)
        }
        Response::Rejected { reason } => Err(reason),
        x => Err(format!("phase_result:{x:?}")),
    }
}

fn handle_client(stream: TcpStream, workers: &[String], timeout: Duration) -> Result<(), String> {
    stream.set_nodelay(true).map_err(|e| e.to_string())?;
    let mut r = BufReader::new(stream.try_clone().map_err(|e| e.to_string())?);
    let mut w = BufWriter::new(stream);
    let mut line = String::new();
    r.read_line(&mut line).map_err(|e| e.to_string())?;
    let req: Request = serde_json::from_str(line.trim_end()).map_err(|e| e.to_string())?;
    if req.protocol != "RVFABRIC_COMFY/1" { return Err("protocol_mismatch".into()); }
    if req.frame_count < 2 || req.target_fps <= req.source_fps || req.target_fps % req.source_fps != 0 {
        return Err("invalid_rate_or_frame_count".into());
    }
    let frame_bytes = req.width.checked_mul(req.height).and_then(|x| x.checked_mul(3)).ok_or("size_overflow")?;
    let mut source = vec![0u8; frame_bytes * req.frame_count];
    r.read_exact(&mut source).map_err(|e| e.to_string())?;
    let frames: Vec<&[u8]> = source.chunks_exact(frame_bytes).collect();
    let step = (req.target_fps / req.source_fps) as usize;
    let phases = phase_positions(req.source_fps, req.target_fps);
    let output_count = (req.frame_count - 1) * step + 1;
    let mut output: Vec<Option<Vec<u8>>> = vec![None; output_count];
    for (i, f) in frames.iter().enumerate() { output[i * step] = Some(f.to_vec()); }
    let mut worker_generated = 0usize;
    let mut fallback = 0usize;
    let mut failures = Vec::new();

    for interval in 0..req.frame_count - 1 {
        let addr = &workers[interval % workers.len()];
        let mut conn = connect(addr, timeout);
        let cache_error = match conn.as_mut() {
            Ok((wr, ww)) => cache_pair(wr, ww, req.generation, interval as u64, req.width, req.height, frames[interval], frames[interval + 1]).err(),
            Err(e) => Some(e.clone()),
        };
        if let Some(e) = cache_error {
            failures.push(format!("interval={interval}:cache:{e}"));
            conn = Err(e);
        }
        for (offset, &phase_u8) in phases.iter().enumerate() {
            let pos = interval * step + offset + 1;
            let produced = if let Ok((ref mut wr, ref mut ww)) = conn {
                phase(wr, ww, req.generation, interval as u64, pos, phase_u8, frame_bytes)
            } else { Err("worker_unavailable".into()) };
            match produced {
                Ok(pixels) => { output[pos] = Some(pixels); worker_generated += 1; }
                Err(e) => {
                    output[pos] = Some(interpolate_rgb_u8(frames[interval], frames[interval + 1], phase_u8));
                    fallback += 1;
                    failures.push(format!("position={pos}:{e}"));
                    conn = Err(e);
                }
            }
        }
    }
    if output.iter().any(|x| x.is_none()) { return Err("output_hole".into()); }
    let reply = Reply {
        protocol: "RVFABRIC_COMFY/1", status: "ok", generation: req.generation,
        width: req.width, height: req.height, output_frames: output_count,
        payload_bytes: output_count * frame_bytes, worker_generated_count: worker_generated,
        fallback_count: fallback, failures,
        claim_scope: "real RGB payload path; simulated worker math is deterministic linear interpolation, not neural reconstruction",
    };
    send_json(&mut w, &reply)?;
    for f in output { w.write_all(f.as_ref().unwrap()).map_err(|e| e.to_string())?; }
    w.flush().map_err(|e| e.to_string())
}

fn main() -> std::io::Result<()> {
    let listen = arg_string("--listen", "127.0.0.1:19000");
    let workers: Vec<String> = arg_string("--workers", "127.0.0.1:19110,127.0.0.1:19111,127.0.0.1:19112,127.0.0.1:19113")
        .split(',').map(|x| x.to_string()).collect();
    let timeout = Duration::from_millis(arg("--timeout-ms", 400u64));
    let listener = TcpListener::bind(&listen)?;
    eprintln!("RVFABRICD_PIXEL_READY listen={listen} workers={workers:?}");
    for stream in listener.incoming() {
        match stream {
            Ok(s) => if let Err(e) = handle_client(s, &workers, timeout) { eprintln!("RVFABRICD_PIXEL_CLIENT_ERROR {e}"); },
            Err(e) => eprintln!("RVFABRICD_PIXEL_ACCEPT_ERROR {e}"),
        }
    }
    Ok(())
}
