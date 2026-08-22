use serde::Serialize;
use std::env;
use std::fs;

#[derive(Debug, Clone, Serialize)]
struct Case {
    source_fps: u32,
    target_fps: u32,
    nodes: usize,
    npu_ms: f64,
    rga_ms: f64,
    vpu_ms: f64,
    jitter_ms_max: f64,
    link_mbps: f64,
    playout_delay_ms: f64,
    endpoint_pair_bytes_per_interval: usize,
    encoded_bytes_per_reconstructed: usize,
    jobs_per_interval: usize,
    reconstructed_jobs: usize,
    neural_on_time_jobs: usize,
    linear_fallback_jobs: usize,
    neural_deadline_success_rate: f64,
    presentation_success_rate_with_linear_fallback: f64,
    max_worker_busy_fraction: f64,
    aggregate_reconstructed_capacity_fps: f64,
    required_reconstructed_fps: f64,
    throughput_sustainable: bool,
    neural_deadline_target_99_9: bool,
    p99_lateness_ms: f64,
    max_lateness_ms: f64,
}

#[derive(Debug, Serialize)]
struct Envelope {
    protocol: &'static str,
    status: &'static str,
    semantics: &'static str,
    assumptions: Assumptions,
    cases: Vec<Case>,
    frontiers: Vec<Frontier>,
    claim_scope: &'static str,
}

#[derive(Debug, Serialize)]
struct Assumptions {
    endpoint_rgb_bytes_each: usize,
    endpoint_pair_bytes_per_interval: usize,
    encoded_bytes_per_reconstructed: usize,
    network_command_roundtrip_ms: f64,
    rga_ms: f64,
    vpu_ms: f64,
    seconds_simulated: u32,
    linear_fallback_preserves_presentation: bool,
}

#[derive(Debug, Serialize)]
struct Frontier {
    source_fps: u32,
    nodes: usize,
    link_mbps: f64,
    jitter_ms_max: f64,
    max_tested_npu_ms_with_99_9_neural_deadlines: Option<f64>,
    max_tested_npu_ms_throughput_sustainable: Option<f64>,
}

fn arg_string(name: &str, default: &str) -> String {
    let args: Vec<String> = env::args().collect();
    args.windows(2)
        .find(|w| w[0] == name)
        .map(|w| w[1].clone())
        .unwrap_or_else(|| default.to_string())
}

fn deterministic_jitter(job_id: u64, max_ms: f64) -> f64 {
    if max_ms <= 0.0 { return 0.0; }
    let x = job_id.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
    let unit = ((x >> 33) as f64) / (u32::MAX as f64);
    unit.min(1.0) * max_ms
}

fn percentile(mut v: Vec<f64>, p: f64) -> f64 {
    if v.is_empty() { return 0.0; }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let idx = ((v.len() - 1) as f64 * p).round() as usize;
    v[idx]
}

fn transfer_ms(bytes: usize, mbps: f64) -> f64 {
    bytes as f64 * 8.0 / (mbps * 1_000_000.0) * 1000.0
}

fn simulate(
    source_fps: u32,
    nodes: usize,
    npu_ms: f64,
    jitter_ms_max: f64,
    link_mbps: f64,
    seconds: u32,
    playout_delay_ms: f64,
) -> Case {
    let target_fps = 60u32;
    let rga_ms = 2.0;
    let vpu_ms = 1.0;
    let network_command_roundtrip_ms = 0.20;
    let endpoint_each = 640usize * 360 * 3;
    let endpoint_pair = endpoint_each * 2;
    let encoded = 20_000usize;
    let step = target_fps / source_fps;
    let jobs_per_interval = (step - 1) as usize;
    let intervals = source_fps as usize * seconds as usize;
    let total_jobs = intervals * jobs_per_interval;
    let frame_period_ms = 1000.0 / target_fps as f64;
    let interval_period_ms = 1000.0 / source_fps as f64;
    let cache_wire_ms = transfer_ms(endpoint_pair, link_mbps) + network_command_roundtrip_ms;
    let result_wire_ms = transfer_ms(encoded, link_mbps) + network_command_roundtrip_ms;
    let mut worker_available = vec![0.0f64; nodes];
    let mut worker_busy = vec![0.0f64; nodes];
    let mut on_time = 0usize;
    let mut lateness = Vec::with_capacity(total_jobs);

    for interval in 0..intervals {
        let worker = interval % nodes;
        let source_arrival_ms = (interval + 1) as f64 * interval_period_ms;
        let mut t = worker_available[worker].max(source_arrival_ms);
        t += cache_wire_ms;
        worker_busy[worker] += cache_wire_ms;
        let left_frame = interval as u64 * step as u64;
        for offset in 1..step {
            let output_frame = left_frame + offset as u64;
            let job_id = interval as u64 * 1000 + output_frame;
            let compute = npu_ms + rga_ms + vpu_ms + deterministic_jitter(job_id, jitter_ms_max);
            t += compute + result_wire_ms;
            worker_busy[worker] += compute + result_wire_ms;
            let deadline = playout_delay_ms + output_frame as f64 * frame_period_ms;
            let late = t - deadline;
            lateness.push(late);
            if late <= 0.0 { on_time += 1; }
        }
        worker_available[worker] = t;
    }

    let duration_ms = seconds as f64 * 1000.0 + playout_delay_ms;
    let max_busy_fraction = worker_busy.iter().map(|x| *x / duration_ms).fold(0.0, f64::max);
    let per_interval_service_at_mean_jitter = cache_wire_ms
        + jobs_per_interval as f64 * (npu_ms + rga_ms + vpu_ms + jitter_ms_max * 0.5 + result_wire_ms);
    let capacity = nodes as f64 * jobs_per_interval as f64 * 1000.0 / per_interval_service_at_mean_jitter;
    let required = (target_fps - source_fps) as f64;
    let success_rate = on_time as f64 / total_jobs as f64;
    let max_late = lateness.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    Case {
        source_fps,
        target_fps,
        nodes,
        npu_ms,
        rga_ms,
        vpu_ms,
        jitter_ms_max,
        link_mbps,
        playout_delay_ms,
        endpoint_pair_bytes_per_interval: endpoint_pair,
        encoded_bytes_per_reconstructed: encoded,
        jobs_per_interval,
        reconstructed_jobs: total_jobs,
        neural_on_time_jobs: on_time,
        linear_fallback_jobs: total_jobs - on_time,
        neural_deadline_success_rate: success_rate,
        presentation_success_rate_with_linear_fallback: 1.0,
        max_worker_busy_fraction: max_busy_fraction,
        aggregate_reconstructed_capacity_fps: capacity,
        required_reconstructed_fps: required,
        throughput_sustainable: capacity >= required,
        neural_deadline_target_99_9: success_rate >= 0.999,
        p99_lateness_ms: percentile(lateness.clone(), 0.99),
        max_lateness_ms: max_late,
    }
}

fn main() {
    let out = arg_string("--out", "F6_SURVIVAL_ENVELOPE.json");
    let source_rates = [5u32, 10, 15];
    let node_counts = [4usize, 6, 8];
    let npu_values = [8.0, 12.0, 16.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 55.0, 65.0, 75.0];
    let links = [100.0, 300.0, 1000.0];
    let jitters = [0.0, 1.0, 5.0, 20.0];
    let seconds = 12u32;
    let playout_delay_ms = 300.0;
    let mut cases = Vec::new();
    for source in source_rates {
        for nodes in node_counts {
            for link in links {
                for jitter in jitters {
                    for npu in npu_values {
                        cases.push(simulate(source, nodes, npu, jitter, link, seconds, playout_delay_ms));
                    }
                }
            }
        }
    }

    let mut frontiers = Vec::new();
    for source in source_rates {
        for nodes in node_counts {
            for link in links {
                for jitter in jitters {
                    let matching: Vec<&Case> = cases.iter().filter(|c|
                        c.source_fps == source && c.nodes == nodes && c.link_mbps == link && c.jitter_ms_max == jitter
                    ).collect();
                    let max_deadline = matching.iter().filter(|c| c.neural_deadline_target_99_9).map(|c| c.npu_ms).reduce(f64::max);
                    let max_throughput = matching.iter().filter(|c| c.throughput_sustainable).map(|c| c.npu_ms).reduce(f64::max);
                    frontiers.push(Frontier {
                        source_fps: source,
                        nodes,
                        link_mbps: link,
                        jitter_ms_max: jitter,
                        max_tested_npu_ms_with_99_9_neural_deadlines: max_deadline,
                        max_tested_npu_ms_throughput_sustainable: max_throughput,
                    });
                }
            }
        }
    }

    let envelope = Envelope {
        protocol: "RVFABRIC_F6_SURVIVAL_ENVELOPE/1",
        status: "MODELED_AGAINST_PRODUCTION_SHAPED_INTERVAL_OWNERSHIP",
        semantics: "each worker caches one authoritative RGB endpoint pair and serially executes every intermediate phase for that interval; missed neural deadlines use deterministic linear fallback without stalling presentation",
        assumptions: Assumptions {
            endpoint_rgb_bytes_each: 640 * 360 * 3,
            endpoint_pair_bytes_per_interval: 2 * 640 * 360 * 3,
            encoded_bytes_per_reconstructed: 20_000,
            network_command_roundtrip_ms: 0.20,
            rga_ms: 2.0,
            vpu_ms: 1.0,
            seconds_simulated: seconds,
            linear_fallback_preserves_presentation: true,
        },
        cases,
        frontiers,
        claim_scope: "deterministic/discrete-event software envelope only; not RV1126 DDR, thermal, NPU, RGA, VPU, or silicon timing evidence",
    };
    fs::write(&out, serde_json::to_vec_pretty(&envelope).unwrap()).unwrap();
    println!("RVFABRIC_F6_ENVELOPE_PASS cases={} frontiers={} out={out}", envelope.cases.len(), envelope.frontiers.len());
}
