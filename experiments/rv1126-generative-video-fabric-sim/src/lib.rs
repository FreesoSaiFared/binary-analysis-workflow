use serde::{Deserialize, Serialize};
use std::collections::HashSet;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Command {
    Hello { protocol: String },
    CacheInterval {
        generation: u64,
        interval_id: u64,
        left_frame_id: u64,
        right_frame_id: u64,
        left_bytes: usize,
        right_bytes: usize,
    },
    CacheIntervalPixels {
        generation: u64,
        interval_id: u64,
        left_frame_id: u64,
        right_frame_id: u64,
        width: usize,
        height: usize,
        left_bytes: usize,
        right_bytes: usize,
    },
    Phase {
        generation: u64,
        interval_id: u64,
        job_id: u64,
        phase_u8: u8,
    },
    PhasePixels {
        generation: u64,
        interval_id: u64,
        job_id: u64,
        phase_u8: u8,
    },
    Reset { generation: u64 },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Response {
    HelloAck { protocol: String },
    PayloadReady { generation: u64, interval_id: u64, total_bytes: usize },
    Cached { generation: u64, interval_id: u64 },
    Result {
        generation: u64,
        interval_id: u64,
        job_id: u64,
        phase_u8: u8,
        simulated_compute_ms: f64,
        encoded_bytes: usize,
    },
    ResultPixels {
        generation: u64,
        interval_id: u64,
        job_id: u64,
        phase_u8: u8,
        simulated_compute_ms: f64,
        payload_bytes: usize,
    },
    ResetAck { generation: u64 },
    Rejected { reason: String },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkerConfig {
    pub npu_ms: f64,
    pub rga_ms: f64,
    pub vpu_ms: f64,
    pub jitter_ms: f64,
    pub encoded_bytes: usize,
    pub drop_every: u64,
    pub stall_every: u64,
    pub stall_ms: f64,
    pub throttle_after_jobs: u64,
    pub throttle_multiplier: f64,
}

impl Default for WorkerConfig {
    fn default() -> Self {
        Self {
            npu_ms: 20.0,
            rga_ms: 2.0,
            vpu_ms: 1.0,
            jitter_ms: 0.0,
            encoded_bytes: 20_000,
            drop_every: 0,
            stall_every: 0,
            stall_ms: 0.0,
            throttle_after_jobs: 0,
            throttle_multiplier: 1.0,
        }
    }
}

#[derive(Debug, Default)]
pub struct WorkerState {
    pub generation: u64,
    pub cached_interval: Option<u64>,
    pub cached_pair: Option<(u64, u64)>,
    pub seen_jobs: HashSet<u64>,
    pub completed_jobs: u64,
}

impl WorkerState {
    pub fn validate_cache_generation(&self, generation: u64) -> Result<(), &'static str> {
        if generation < self.generation {
            Err("stale_generation")
        } else {
            Ok(())
        }
    }

    pub fn handle_cache(
        &mut self,
        generation: u64,
        interval_id: u64,
        left_frame_id: u64,
        right_frame_id: u64,
    ) -> Response {
        if generation < self.generation {
            return Response::Rejected { reason: "stale_generation".into() };
        }
        if generation > self.generation {
            self.generation = generation;
            self.cached_interval = None;
            self.cached_pair = None;
            self.seen_jobs.clear();
        }
        self.cached_interval = Some(interval_id);
        self.cached_pair = Some((left_frame_id, right_frame_id));
        Response::Cached { generation, interval_id }
    }

    pub fn validate_phase(
        &mut self,
        generation: u64,
        interval_id: u64,
        job_id: u64,
    ) -> Result<(), &'static str> {
        if generation != self.generation {
            return Err("generation_mismatch");
        }
        if self.cached_interval != Some(interval_id) {
            return Err("cache_miss_or_overwrite");
        }
        if !self.seen_jobs.insert(job_id) {
            return Err("duplicate_job");
        }
        Ok(())
    }

    pub fn reset(&mut self, generation: u64) -> Response {
        if generation < self.generation {
            return Response::Rejected { reason: "stale_reset".into() };
        }
        self.generation = generation;
        self.cached_interval = None;
        self.cached_pair = None;
        self.seen_jobs.clear();
        Response::ResetAck { generation }
    }
}

pub fn phase_positions(source_fps: u32, target_fps: u32) -> Vec<u8> {
    assert!(target_fps > source_fps && target_fps % source_fps == 0);
    let step = target_fps / source_fps;
    (1..step)
        .map(|offset| ((255.0 * offset as f64 / step as f64).round() as u16).min(255) as u8)
        .collect()
}

pub fn interpolate_rgb_u8(left: &[u8], right: &[u8], phase_u8: u8) -> Vec<u8> {
    assert_eq!(left.len(), right.len());
    let p = phase_u8 as u32;
    let q = 255u32 - p;
    left.iter()
        .zip(right)
        .map(|(&a, &b)| (((a as u32) * q + (b as u32) * p + 127) / 255) as u8)
        .collect()
}

pub fn interval_jobs_per_second(source_fps: u32, target_fps: u32) -> f64 {
    (target_fps - source_fps) as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_generation_is_rejected() {
        let mut s = WorkerState::default();
        let _ = s.handle_cache(4, 1, 10, 20);
        assert!(matches!(s.handle_cache(3, 2, 20, 30), Response::Rejected { .. }));
        assert_eq!(s.validate_cache_generation(3), Err("stale_generation"));
    }

    #[test]
    fn cache_overwrite_rejects_old_interval() {
        let mut s = WorkerState::default();
        let _ = s.handle_cache(1, 7, 70, 80);
        let _ = s.handle_cache(1, 8, 80, 90);
        assert_eq!(s.validate_phase(1, 7, 100), Err("cache_miss_or_overwrite"));
    }

    #[test]
    fn duplicate_job_is_rejected() {
        let mut s = WorkerState::default();
        let _ = s.handle_cache(1, 7, 70, 80);
        assert_eq!(s.validate_phase(1, 7, 100), Ok(()));
        assert_eq!(s.validate_phase(1, 7, 100), Err("duplicate_job"));
    }

    #[test]
    fn phase_map_matches_expected_intermediates() {
        assert_eq!(phase_positions(15, 60), vec![64, 128, 191]);
        assert_eq!(phase_positions(10, 60).len(), 5);
        assert_eq!(phase_positions(5, 60).len(), 11);
    }

    #[test]
    fn pixel_interpolation_is_integer_deterministic() {
        let left = [0u8, 10, 100, 255];
        let right = [255u8, 110, 200, 0];
        assert_eq!(interpolate_rgb_u8(&left, &right, 0), left);
        assert_eq!(interpolate_rgb_u8(&left, &right, 255), right);
        assert_eq!(interpolate_rgb_u8(&left, &right, 128), vec![128, 60, 150, 127]);
    }
}
