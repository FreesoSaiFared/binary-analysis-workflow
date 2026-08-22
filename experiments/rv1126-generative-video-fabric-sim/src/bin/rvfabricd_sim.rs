use rvfabric_sim::{phase_positions, Command, Response};
use serde::Serialize;
use std::env;
use std::fs;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Clone)]
struct IntervalTask {
    generation: u64,
    interval_id: u64,
    left_frame_id: u64,
    right_frame_id: u64,
    phases: Vec<(u64, u8)>,
}

#[derive(Debug, Serialize)]
struct JobResult {
    worker: usize,
    interval_id: u64,
    job_id: u64,
    output_frame_id: u64,
    phase_u8: u8,
    neural_ready_ms: Option<f64>,
    presentation_deadline_ms: f64,
    neural_on_time: bool,
    fallback_used: bool,
    worker_reported_compute_ms: Option<f64>,
    encoded_bytes: usize,
    failure: Option<String>,
}

#[derive(Debug, Serialize)]
struct Summary {
    protocol: &'static str,
    status: &'static str,
    source_fps: u32,
    target_fps: u32,
    seconds: u32,
    workers: usize,
    reconstructed_jobs: usize,
    neural_on_time: usize,
    linear_fallbacks: usize,
    presentation_success: usize,
    presentation_success_rate: f64,
    neural_deadline_success_rate: f64,
    observed_wall_ms: f64,
    effective_reconstructed_fps: f64,
    encoded_output_bytes: usize,
    p50_neural_ready_ms: Option<f64>,
    p95_neural_ready_ms: Option<f64>,
    p99_neural_ready_ms: Option<f64>,
    max_neural_ready_ms: Option<f64>,
    stale_generation_probe: String,
    claim_scope: &'static str,
    results: Vec<JobResult>,
}

fn arg<T: std::str::FromStr>(name: &str, default: T) -> T {
    let args: Vec<String> = env::args().collect();
    args.windows(2)
        .find(|w| w[0] == name)
        .and_then(|w| w[1].parse::<T>().ok())
        .unwrap_or(default)
}

fn arg_string(name: &str, default: &str) -> String {
    let args: Vec<String> = env::args().collect();
    args.windows(2)
        .find(|w| w[0] == name)
        .map(|w| w[1].clone())
        .unwrap_or_else(|| default.to_string())
}

fn send_command(writer: &mut BufWriter<TcpStream>, cmd: &Command) -> std::io::Result<()> {
    serde_json::to_writer(&mut *writer, cmd)?;
    writer.write_all(b"\n")?;
    writer.flush()
}

fn read_response(reader: &mut BufReader<TcpStream>) -> Result<Response, String> {
    let mut line = String::new();
    let n = reader.read_line(&mut line).map_err(|e| format!("read:{e}"))?;
    if n == 0 { return Err("eof".into()); }
    serde_json::from_str(line.trim_end()).map_err(|e| format!("json:{e}"))
}

fn connect(addr: &str, timeout: Duration) -> Result<(BufReader<TcpStream>, BufWriter<TcpStream>), String> {
    let socket = addr.to_socket_addrs().map_err(|e| e.to_string())?.next().ok_or("no_address")?;
    let stream = TcpStream::connect_timeout(&socket, timeout).map_err(|e| format!("connect:{e}"))?;
    stream.set_nodelay(true).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(timeout)).map_err(|e| e.to_string())?;
    stream.set_write_timeout(Some(timeout)).map_err(|e| e.to_string())?;
    let reader = BufReader::new(stream.try_clone().map_err(|e| e.to_string())?);
    let mut writer = BufWriter::new(stream);
    send_command(&mut writer, &Command::Hello { protocol: "RVFABRIC/1".into() }).map_err(|e| e.to_string())?;
    let mut r = reader;
    match read_response(&mut r)? {
        Response::HelloAck { .. } => Ok((r, writer)),
        other => Err(format!("hello_rejected:{other:?}")),
    }
}

fn percentile(mut v: Vec<f64>, p: f64) -> Option<f64> {
    if v.is_empty() { return None; }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let idx = ((v.len() - 1) as f64 * p).round() as usize;
    Some(v[idx])
}

fn worker_thread(
    worker_id: usize,
    addr: String,
    rx: mpsc::Receiver<IntervalTask>,
    tx: mpsc::Sender<JobResult>,
    start: Arc<Instant>,
    timeout: Duration,
    frame_period_ms: f64,
    playout_delay_ms: f64,
    left_bytes: usize,
    right_bytes: usize,
) {
    let mut connection: Option<(BufReader<TcpStream>, BufWriter<TcpStream>)> = None;
    for task in rx {
        if connection.is_none() {
            connection = connect(&addr, timeout).ok();
        }
        let cache_ok = if let Some((reader, writer)) = connection.as_mut() {
            let cmd = Command::CacheInterval {
                generation: task.generation,
                interval_id: task.interval_id,
                left_frame_id: task.left_frame_id,
                right_frame_id: task.right_frame_id,
                left_bytes,
                right_bytes,
            };
            if send_command(writer, &cmd).is_err() {
                false
            } else {
                matches!(read_response(reader), Ok(Response::Cached { .. }))
            }
        } else { false };

        if !cache_ok {
            connection = None;
            for (output_frame_id, phase_u8) in &task.phases {
                let job_id = task.interval_id * 1000 + *output_frame_id;
                let deadline = playout_delay_ms + *output_frame_id as f64 * frame_period_ms;
                let _ = tx.send(JobResult {
                    worker: worker_id,
                    interval_id: task.interval_id,
                    job_id,
                    output_frame_id: *output_frame_id,
                    phase_u8: *phase_u8,
                    neural_ready_ms: None,
                    presentation_deadline_ms: deadline,
                    neural_on_time: false,
                    fallback_used: true,
                    worker_reported_compute_ms: None,
                    encoded_bytes: 0,
                    failure: Some("cache_or_connect_failure".into()),
                });
            }
            continue;
        }

        for (output_frame_id, phase_u8) in &task.phases {
            let job_id = task.interval_id * 1000 + *output_frame_id;
            let deadline = playout_delay_ms + *output_frame_id as f64 * frame_period_ms;
            let mut failure = None;
            let mut reported = None;
            let mut encoded = 0usize;
            let mut ready = None;
            if let Some((reader, writer)) = connection.as_mut() {
                let cmd = Command::Phase {
                    generation: task.generation,
                    interval_id: task.interval_id,
                    job_id,
                    phase_u8: *phase_u8,
                };
                match send_command(writer, &cmd) {
                    Ok(()) => match read_response(reader) {
                        Ok(Response::Result { simulated_compute_ms, encoded_bytes, .. }) => {
                            ready = Some(start.elapsed().as_secs_f64() * 1000.0);
                            reported = Some(simulated_compute_ms);
                            encoded = encoded_bytes;
                        }
                        Ok(Response::Rejected { reason }) => failure = Some(format!("rejected:{reason}")),
                        Ok(other) => failure = Some(format!("unexpected:{other:?}")),
                        Err(e) => failure = Some(e),
                    },
                    Err(e) => failure = Some(format!("write:{e}")),
                }
            } else {
                failure = Some("disconnected".into());
            }
            if failure.is_some() { connection = None; }
            let on_time = ready.map(|r| r <= deadline).unwrap_or(false);
            let _ = tx.send(JobResult {
                worker: worker_id,
                interval_id: task.interval_id,
                job_id,
                output_frame_id: *output_frame_id,
                phase_u8: *phase_u8,
                neural_ready_ms: ready,
                presentation_deadline_ms: deadline,
                neural_on_time: on_time,
                fallback_used: !on_time,
                worker_reported_compute_ms: reported,
                encoded_bytes: encoded,
                failure,
            });
        }
    }
}

fn stale_probe(addr: &str, timeout: Duration, generation: u64) -> String {
    if generation == 0 { return "UNAVAILABLE:generation_zero".into(); }
    match connect(addr, timeout) {
        Ok((mut reader, mut writer)) => {
            let stale = Command::CacheInterval { generation: generation - 1, interval_id: u64::MAX - 1, left_frame_id: 3, right_frame_id: 4, left_bytes: 1, right_bytes: 1 };
            if send_command(&mut writer, &stale).is_err() { return "probe_write_fail".into(); }
            match read_response(&mut reader) {
                Ok(Response::Rejected { reason }) if reason == "stale_generation" => "PASS".into(),
                other => format!("FAIL:{other:?}"),
            }
        }
        Err(e) => format!("UNAVAILABLE:{e}"),
    }
}

fn main() {
    let workers_arg = arg_string("--workers", "127.0.0.1:19110,127.0.0.1:19111,127.0.0.1:19112,127.0.0.1:19113");
    let workers: Vec<String> = workers_arg.split(',').map(|s| s.to_string()).collect();
    let source_fps: u32 = arg("--source-fps", 10u32);
    let target_fps: u32 = arg("--target-fps", 60u32);
    let seconds: u32 = arg("--seconds", 6u32);
    let generation: u64 = arg("--generation", 1u64);
    let timeout_ms: u64 = arg("--timeout-ms", 250u64);
    let playout_delay_ms: f64 = arg("--playout-delay-ms", 500.0);
    let left_bytes: usize = arg("--left-bytes", 691_200usize);
    let right_bytes: usize = arg("--right-bytes", 691_200usize);
    let out = arg_string("--out", "F6_RUNTIME_RESULT.json");
    assert!(!workers.is_empty());
    assert!(target_fps > source_fps && target_fps % source_fps == 0);

    let start = Arc::new(Instant::now());
    let timeout = Duration::from_millis(timeout_ms);
    let frame_period_ms = 1000.0 / target_fps as f64;
    let phases = phase_positions(source_fps, target_fps);
    let step = target_fps / source_fps;
    let interval_count = source_fps as usize * seconds as usize;
    let expected_jobs = interval_count * phases.len();
    let (result_tx, result_rx) = mpsc::channel::<JobResult>();
    let mut task_txs = Vec::new();
    let mut handles = Vec::new();

    for (id, addr) in workers.iter().enumerate() {
        let (tx, rx) = mpsc::channel::<IntervalTask>();
        task_txs.push(tx);
        let rtx = result_tx.clone();
        let s = start.clone();
        let addr2 = addr.clone();
        handles.push(thread::spawn(move || worker_thread(
            id, addr2, rx, rtx, s, timeout, frame_period_ms, playout_delay_ms, left_bytes, right_bytes,
        )));
    }
    drop(result_tx);

    for interval in 0..interval_count {
        let left = interval as u64 * step as u64;
        let right = left + step as u64;
        let interval_phases: Vec<(u64, u8)> = phases.iter().enumerate()
            .map(|(i, p)| (left + i as u64 + 1, *p)).collect();
        let task = IntervalTask {
            generation,
            interval_id: interval as u64,
            left_frame_id: left,
            right_frame_id: right,
            phases: interval_phases,
        };
        task_txs[interval % workers.len()].send(task).unwrap();
    }
    drop(task_txs);

    let mut results: Vec<JobResult> = result_rx.into_iter().collect();
    for h in handles { let _ = h.join(); }
    results.sort_by_key(|r| r.output_frame_id);
    assert_eq!(results.len(), expected_jobs);
    let wall_ms = start.elapsed().as_secs_f64() * 1000.0;
    let neural_on_time = results.iter().filter(|r| r.neural_on_time).count();
    let fallbacks = expected_jobs - neural_on_time;
    let ready: Vec<f64> = results.iter().filter_map(|r| r.neural_ready_ms).collect();
    let encoded_output_bytes = results.iter().map(|r| r.encoded_bytes).sum();
    let stale_generation_probe = stale_probe(&workers[0], timeout, generation);
    let summary = Summary {
        protocol: "RVFABRIC_F6_RUNTIME/1",
        status: if stale_generation_probe == "PASS" { "RUNTIME_COMPLETED" } else { "RUNTIME_COMPLETED_WITH_PROTOCOL_PROBE_FAILURE" },
        source_fps,
        target_fps,
        seconds,
        workers: workers.len(),
        reconstructed_jobs: expected_jobs,
        neural_on_time,
        linear_fallbacks: fallbacks,
        presentation_success: expected_jobs,
        presentation_success_rate: 1.0,
        neural_deadline_success_rate: neural_on_time as f64 / expected_jobs as f64,
        observed_wall_ms: wall_ms,
        effective_reconstructed_fps: expected_jobs as f64 / (wall_ms / 1000.0),
        encoded_output_bytes,
        p50_neural_ready_ms: percentile(ready.clone(), 0.50),
        p95_neural_ready_ms: percentile(ready.clone(), 0.95),
        p99_neural_ready_ms: percentile(ready.clone(), 0.99),
        max_neural_ready_ms: ready.into_iter().reduce(f64::max),
        stale_generation_probe,
        claim_scope: "loopback production-shaped host/worker runtime; observed wall time is host diagnostic, not RV1126 silicon timing",
        results,
    };
    fs::write(&out, serde_json::to_vec_pretty(&summary).unwrap()).unwrap();
    println!("{}", serde_json::to_string(&summary).unwrap());
    println!("RVFABRIC_F6_RUNTIME_COMPLETE out={out}");
}
