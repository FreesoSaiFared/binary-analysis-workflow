#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define PAGE_SZ 4096u
#define DONE_MAGIC 0x600df00dU
#define RK_IOC_MAGIC 'R'
struct rk_user_xfer { uint64_t user_ptr; uint64_t latency_ns; uint32_t from; uint32_t irq_count; };
struct rk_status { uint32_t id; uint32_t owner; uint32_t state; uint32_t irq_count; uint32_t done; uint32_t reserved; uint64_t final_checksum; };
#define RK_IOCTL_WAIT_READY _IO(RK_IOC_MAGIC, 1)
#define RK_IOCTL_GET_STATUS _IOR(RK_IOC_MAGIC, 2, struct rk_status)
#define RK_IOCTL_ACQUIRE _IOWR(RK_IOC_MAGIC, 3, struct rk_user_xfer)
#define RK_IOCTL_COMMIT _IOW(RK_IOC_MAGIC, 4, struct rk_user_xfer)
#define RK_IOCTL_WAIT_DONE _IO(RK_IOC_MAGIC, 5)

static int devfd = -1;
static uint8_t *fault_page;
static unsigned my_id;
static volatile sig_atomic_t fault_count;
static volatile uint64_t fault_latency_ns;
static volatile unsigned fault_from;

static const uint64_t expected_before[4] = {
    0, 0x2b8a688f71a14b75ULL, 0x5a6d36bdedcfb892ULL, 0x3d49376b6dab225bULL
};
static const uint64_t expected_after[4] = {
    0, 0x5a6d36bdedcfb892ULL, 0x3d49376b6dab225bULL, 0xc52f003769448c32ULL
};

static uint64_t checksum_page(const uint8_t *p)
{
    uint64_t h = 0xcbf29ce484222325ULL;
    unsigned i;
    for (i = 0; i < PAGE_SZ; i++) {
        h ^= p[i];
        h *= 0x100000001b3ULL;
    }
    return h;
}

static uint64_t mix(uint64_t x, unsigned id)
{
    x ^= 0x9e3779b97f4a7c15ULL + id + (x << 6) + (x >> 2);
    return x * 0xbf58476d1ce4e5b9ULL;
}

static void die(const char *m)
{
    perror(m);
    _exit(80);
}

static void segv_handler(int sig, siginfo_t *si, void *ctx)
{
    struct rk_user_xfer x;
    uintptr_t a = (uintptr_t)si->si_addr;
    uintptr_t b = (uintptr_t)fault_page;
    (void)sig;
    (void)ctx;

    if (a < b || a >= b + PAGE_SZ || my_id == 0)
        _exit(91);
    if (fault_count)
        _exit(92);
    fault_count = 1;
    dprintf(1, "RKMESH_V2_FAULT_SIGNAL id=%u count=1\n", my_id);

    if (mprotect(fault_page, PAGE_SZ, PROT_READ | PROT_WRITE))
        _exit(93);
    memset(&x, 0, sizeof(x));
    x.user_ptr = (uintptr_t)fault_page;
    if (ioctl(devfd, RK_IOCTL_ACQUIRE, &x))
        _exit(94);
    fault_latency_ns = x.latency_ns;
    fault_from = x.from;
}

static void setup_fs(void)
{
    mkdir("/proc", 0555);
    mkdir("/sys", 0555);
    mkdir("/dev", 0755);
    if (mount("proc", "/proc", "proc", 0, 0) && errno != EBUSY)
        die("mount proc");
    if (mount("sysfs", "/sys", "sysfs", 0, 0) && errno != EBUSY)
        die("mount sys");
    if (mount("devtmpfs", "/dev", "devtmpfs", 0, 0) && errno != EBUSY)
        die("mount dev");
}

static void load_module(void)
{
    int fd = open("/rkmesh_page.ko", O_RDONLY);
    int rc, e;
    if (fd < 0)
        die("module open");
    rc = syscall(SYS_finit_module, fd, "", 0);
    e = errno;
    close(fd);
    printf("RKMESH_V2_MODULE_LOAD rc=%d errno=%d\n", rc, e);
    if (rc)
        _exit(81);
}

static int open_device(void)
{
    int i, fd;
    for (i = 0; i < 1000; i++) {
        fd = open("/dev/rkmesh_page", O_RDWR);
        if (fd >= 0)
            return fd;
        usleep(1000);
    }
    return -1;
}

int main(void)
{
    struct rk_status st;
    struct sigaction sa;
    struct rk_user_xfer x;
    uint64_t before, after;

    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    setup_fs();
    load_module();
    devfd = open_device();
    if (devfd < 0)
        die("open rkmesh_page");
    if (ioctl(devfd, RK_IOCTL_WAIT_READY))
        die("wait ready");
    if (ioctl(devfd, RK_IOCTL_GET_STATUS, &st))
        die("get status");
    my_id = st.id;
    printf("RKMESH_V2_USER_READY id=%u owner=%u state=%u\n", my_id, st.owner, st.state);

    if (my_id > 0) {
        fault_page = mmap(NULL, PAGE_SZ, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (fault_page == MAP_FAILED)
            die("mmap fault page");
        memset(&sa, 0, sizeof(sa));
        sa.sa_sigaction = segv_handler;
        sa.sa_flags = SA_SIGINFO;
        sigemptyset(&sa.sa_mask);
        if (sigaction(SIGSEGV, &sa, NULL))
            die("sigaction");

        (void)((volatile uint64_t *)fault_page)[0];
        if (fault_count != 1)
            _exit(82);
        before = checksum_page(fault_page);
        if (before != expected_before[my_id]) {
            fprintf(stderr, "RKMESH_V2_FAIL id=%u reason=bad_before got=0x%016llx expected=0x%016llx\n",
                    my_id, (unsigned long long)before,
                    (unsigned long long)expected_before[my_id]);
            _exit(83);
        }
        ((uint64_t *)fault_page)[my_id] = mix(((uint64_t *)fault_page)[my_id - 1], my_id);
        after = checksum_page(fault_page);
        if (after != expected_after[my_id]) {
            fprintf(stderr, "RKMESH_V2_FAIL id=%u reason=bad_after got=0x%016llx expected=0x%016llx\n",
                    my_id, (unsigned long long)after,
                    (unsigned long long)expected_after[my_id]);
            _exit(84);
        }
        memset(&x, 0, sizeof(x));
        x.user_ptr = (uintptr_t)fault_page;
        if (ioctl(devfd, RK_IOCTL_COMMIT, &x))
            die("commit");
        printf("RKMESH_V2_FAULT_RESUMED id=%u from=%u faults=%d latency_ns=%llu before=0x%016llx after=0x%016llx\n",
               my_id, fault_from, (int)fault_count,
               (unsigned long long)fault_latency_ns,
               (unsigned long long)before, (unsigned long long)after);
    }

    if (ioctl(devfd, RK_IOCTL_WAIT_DONE))
        die("wait done");
    if (ioctl(devfd, RK_IOCTL_GET_STATUS, &st))
        die("get final status");
    if (st.done != DONE_MAGIC || st.final_checksum != 0xc52f003769448c32ULL) {
        fprintf(stderr, "RKMESH_V2_FAIL id=%u reason=final done=0x%x sum=0x%016llx\n",
                my_id, st.done, (unsigned long long)st.final_checksum);
        _exit(85);
    }
    printf("RKMESH_V2_PASS id=%u irq_count=%u final=0x%016llx\n",
           my_id, st.irq_count, (unsigned long long)st.final_checksum);
    sync();
    reboot(RB_POWER_OFF);
    for (;;)
        pause();
}
