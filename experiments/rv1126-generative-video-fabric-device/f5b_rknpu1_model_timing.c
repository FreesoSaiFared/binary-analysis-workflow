/*
 * F5B original-RV1126 model-only timing harness for Rockchip RKNPU1.
 *
 * Build this ON/for the RV1126 SDK against the original rknn_runtime.h and
 * librknn_runtime/librknn_api supplied for linux-armhf-puma.  The input file
 * must be one exact F4 semantic tensor: 1x360x640x7 uint8 NHWC.
 *
 * This reports two clocks separately:
 *   1) RKNN_QUERY_PERF_RUN.run_duration -- Rockchip-defined real inference time (us)
 *   2) CLOCK_MONOTONIC_RAW around inputs_set + run + outputs_get -- process-side time
 * Neither number by itself proves the complete 1080p60 video pipeline.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "rknn_runtime.h"

#define F5_W 640u
#define F5_H 360u
#define F5_C 7u
#define F5_INPUT_BYTES (F5_W * F5_H * F5_C)

static void die(const char *msg, int ret) {
  fprintf(stderr, "F5B_FATAL %s ret=%d errno=%d\n", msg, ret, errno);
  exit(2);
}

static unsigned char *read_exact_file(const char *path, size_t *size_out) {
  FILE *f = fopen(path, "rb");
  if (!f) die("fopen", -1);
  if (fseek(f, 0, SEEK_END) != 0) die("fseek_end", -1);
  long n = ftell(f);
  if (n < 0) die("ftell", -1);
  if (fseek(f, 0, SEEK_SET) != 0) die("fseek_set", -1);
  unsigned char *buf = (unsigned char *)malloc((size_t)n);
  if (!buf) die("malloc", -1);
  if (fread(buf, 1, (size_t)n, f) != (size_t)n) die("fread", -1);
  fclose(f);
  *size_out = (size_t)n;
  return buf;
}

static int64_t ns_now(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC_RAW, &ts) != 0) die("clock_gettime", -1);
  return (int64_t)ts.tv_sec * INT64_C(1000000000) + ts.tv_nsec;
}

static int cmp_i64(const void *a, const void *b) {
  const int64_t x = *(const int64_t *)a;
  const int64_t y = *(const int64_t *)b;
  return (x > y) - (x < y);
}

static int64_t percentile_i64(const int64_t *sorted, int n, double p) {
  if (n <= 0) return 0;
  double pos = p * (double)(n - 1);
  int idx = (int)(pos + 0.5);
  if (idx < 0) idx = 0;
  if (idx >= n) idx = n - 1;
  return sorted[idx];
}

static void print_tensor_attr_json(const char *key, const rknn_tensor_attr *a) {
  printf("\"%s\":{\"index\":%u,\"n_dims\":%u,\"dims\":[", key, a->index, a->n_dims);
  for (uint32_t i = 0; i < a->n_dims; ++i) {
    if (i) putchar(',');
    printf("%u", a->dims[i]);
  }
  printf("],\"n_elems\":%u,\"size\":%u,\"fmt\":%d,\"type\":%d,\"qnt_type\":%d,\"zp\":%u,\"scale\":%.9g}",
         a->n_elems, a->size, (int)a->fmt, (int)a->type, (int)a->qnt_type, a->zp, a->scale);
}

static void one_inference(rknn_context ctx, unsigned char *input,
                          int64_t *perf_run_us, int64_t *process_us,
                          int keep_output, const char *output_path) {
  rknn_input in;
  memset(&in, 0, sizeof(in));
  in.index = 0;
  in.buf = input;
  in.size = F5_INPUT_BYTES;
  in.pass_through = 0;
  in.type = RKNN_TENSOR_UINT8;
  in.fmt = RKNN_TENSOR_NHWC;

  int64_t t0 = ns_now();
  int ret = rknn_inputs_set(ctx, 1, &in);
  if (ret != RKNN_SUCC) die("rknn_inputs_set", ret);
  ret = rknn_run(ctx, NULL);
  if (ret != RKNN_SUCC) die("rknn_run", ret);

  rknn_output out;
  memset(&out, 0, sizeof(out));
  out.index = 0;
  out.want_float = 1;
  out.is_prealloc = 0;
  ret = rknn_outputs_get(ctx, 1, &out, NULL);
  if (ret != RKNN_SUCC) die("rknn_outputs_get", ret);
  int64_t t1 = ns_now();

  rknn_perf_run perf;
  memset(&perf, 0, sizeof(perf));
  ret = rknn_query(ctx, RKNN_QUERY_PERF_RUN, &perf, sizeof(perf));
  if (ret != RKNN_SUCC) die("RKNN_QUERY_PERF_RUN", ret);

  *perf_run_us = perf.run_duration;
  *process_us = (t1 - t0) / 1000;

  if (keep_output && output_path) {
    FILE *f = fopen(output_path, "wb");
    if (!f) die("open_output", -1);
    if (fwrite(out.buf, 1, out.size, f) != out.size) die("write_output", -1);
    fclose(f);
    fprintf(stderr, "F5B_FIRST_FLOAT_OUTPUT bytes=%u path=%s\n", out.size, output_path);
  }

  ret = rknn_outputs_release(ctx, 1, &out);
  if (ret != RKNN_SUCC) die("rknn_outputs_release", ret);
}

int main(int argc, char **argv) {
  if (argc < 3 || argc > 6) {
    fprintf(stderr, "usage: %s MODEL.rknn INPUT_1x360x640x7_U8_NHWC.bin [loops=200] [warmup=20] [first_output_f32.bin]\n", argv[0]);
    return 2;
  }
  const char *model_path = argv[1];
  const char *input_path = argv[2];
  int loops = argc >= 4 ? atoi(argv[3]) : 200;
  int warmup = argc >= 5 ? atoi(argv[4]) : 20;
  const char *first_output = argc >= 6 ? argv[5] : NULL;
  if (loops < 10 || warmup < 0) die("invalid_loop_counts", -1);

  size_t model_size = 0, input_size = 0;
  unsigned char *model = read_exact_file(model_path, &model_size);
  unsigned char *input = read_exact_file(input_path, &input_size);
  if (model_size != 39074u) die("sealed_F4_model_size_mismatch", -1);
  if (input_size != F5_INPUT_BYTES) die("semantic_input_size_mismatch", -1);

  rknn_context ctx = 0;
  int ret = rknn_init(&ctx, model, (uint32_t)model_size, 0);
  if (ret != RKNN_SUCC) die("rknn_init", ret);

  rknn_sdk_version ver;
  memset(&ver, 0, sizeof(ver));
  ret = rknn_query(ctx, RKNN_QUERY_SDK_VERSION, &ver, sizeof(ver));
  if (ret != RKNN_SUCC) die("RKNN_QUERY_SDK_VERSION", ret);

  rknn_input_output_num io;
  memset(&io, 0, sizeof(io));
  ret = rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io, sizeof(io));
  if (ret != RKNN_SUCC) die("RKNN_QUERY_IN_OUT_NUM", ret);
  if (io.n_input != 1 || io.n_output != 1) die("unexpected_io_count", -1);

  rknn_tensor_attr in_attr, out_attr;
  memset(&in_attr, 0, sizeof(in_attr));
  memset(&out_attr, 0, sizeof(out_attr));
  in_attr.index = 0;
  out_attr.index = 0;
  ret = rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &in_attr, sizeof(in_attr));
  if (ret != RKNN_SUCC) die("RKNN_QUERY_INPUT_ATTR", ret);
  ret = rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &out_attr, sizeof(out_attr));
  if (ret != RKNN_SUCC) die("RKNN_QUERY_OUTPUT_ATTR", ret);
  if (in_attr.n_elems != F5_W * F5_H * F5_C) die("input_element_count_mismatch", -1);

  int64_t dummy_perf = 0, dummy_process = 0;
  for (int i = 0; i < warmup; ++i)
    one_inference(ctx, input, &dummy_perf, &dummy_process, 0, NULL);

  int64_t *perf_us = (int64_t *)calloc((size_t)loops, sizeof(int64_t));
  int64_t *process_us = (int64_t *)calloc((size_t)loops, sizeof(int64_t));
  if (!perf_us || !process_us) die("calloc_timings", -1);

  for (int i = 0; i < loops; ++i)
    one_inference(ctx, input, &perf_us[i], &process_us[i], i == 0 && first_output, first_output);

  qsort(perf_us, (size_t)loops, sizeof(int64_t), cmp_i64);
  qsort(process_us, (size_t)loops, sizeof(int64_t), cmp_i64);

  printf("{\"protocol\":\"RV1126_F5B_RKNPU1_MODEL_TIMING/1\",\"status\":\"REAL_DEVICE_MODEL_ONLY_TIMING_MEASURED\",");
  printf("\"scope\":{\"full_pipeline_1080p60\":\"NOT_PROVEN\",\"host_adb_timing\":\"NOT_USED_FOR_SILICON_CLAIM\"},");
  printf("\"sdk\":{\"api_version\":\"%s\",\"driver_version\":\"%s\"},", ver.api_version, ver.drv_version);
  print_tensor_attr_json("input_attr", &in_attr); putchar(',');
  print_tensor_attr_json("output_attr", &out_attr); putchar(',');
  printf("\"warmup\":%d,\"loops\":%d,", warmup, loops);
  printf("\"rknn_query_perf_run_us\":{\"meaning\":\"Rockchip RKNPU1 rknn_perf_run.run_duration: real inference time (us)\",\"p50\":%" PRId64 ",\"p95\":%" PRId64 ",\"p99\":%" PRId64 ",\"min\":%" PRId64 ",\"max\":%" PRId64 "},",
         percentile_i64(perf_us, loops, 0.50), percentile_i64(perf_us, loops, 0.95), percentile_i64(perf_us, loops, 0.99), perf_us[0], perf_us[loops-1]);
  printf("\"onboard_process_us\":{\"meaning\":\"CLOCK_MONOTONIC_RAW around inputs_set+run+outputs_get\",\"p50\":%" PRId64 ",\"p95\":%" PRId64 ",\"p99\":%" PRId64 ",\"min\":%" PRId64 ",\"max\":%" PRId64 "}}\n",
         percentile_i64(process_us, loops, 0.50), percentile_i64(process_us, loops, 0.95), percentile_i64(process_us, loops, 0.99), process_us[0], process_us[loops-1]);

  free(perf_us);
  free(process_us);
  free(input);
  free(model);
  rknn_destroy(ctx);
  return 0;
}
