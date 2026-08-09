#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define PAGE_SZ 4096u
#define BAR2_LEN (16u*1024u*1024u)
#define READY_BASE 0x0000u
#define INIT_OFF 0x0040u
#define OWNER_OFF 0x0044u
#define STATE_OFF 0x0048u
#define REQUESTER_OFF 0x004cu
#define SEQ_OFF 0x0050u
#define TRANSFER_OFF 0x1000u
#define DONE_OFF 0x3000u
#define READY_MAGIC 0x35880000u
#define INIT_MAGIC 0x51a7e001u
#define STATE_IDLE 0u
#define STATE_REQ 1u
#define STATE_RESP 2u
#define STATE_ACK 3u
#define DONE_MAGIC 0x600df00du

static volatile uint8_t *bar2;
static uint8_t *fault_page;
static unsigned my_id;
static volatile sig_atomic_t fault_count;
static volatile uint64_t fault_latency_ns;
static volatile unsigned fault_from;

static inline volatile uint32_t *mmio32(unsigned off){ return (volatile uint32_t *)(bar2+off); }
static inline void fence(void){ __sync_synchronize(); }
static uint64_t nsec_now(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC_RAW,&t); return (uint64_t)t.tv_sec*1000000000ull+(uint64_t)t.tv_nsec; }
static void die(const char *m){ perror(m); _exit(80); }
static int wait32(volatile uint32_t *p,uint32_t v,unsigned ms){
    uint64_t end=nsec_now()+(uint64_t)ms*1000000ull;
    while(nsec_now()<end){ if(*p==v){ fence(); return 0; } usleep(1000); }
    return -1;
}
static uint64_t checksum_page(const uint8_t *p){
    uint64_t h=0xcbf29ce484222325ull;
    for(unsigned i=0;i<PAGE_SZ;i++){ h^=p[i]; h*=0x100000001b3ull; }
    return h;
}
static uint64_t mix(uint64_t x,unsigned id){ x^=0x9e3779b97f4a7c15ull+id+(x<<6)+(x>>2); return x*0xbf58476d1ce4e5b9ull; }

static void segv_handler(int sig, siginfo_t *si, void *ctx){
    (void)sig; (void)ctx;
    uintptr_t a=(uintptr_t)si->si_addr, b=(uintptr_t)fault_page;
    if(a<b || a>=b+PAGE_SZ || my_id==0) _exit(91);
    if(fault_count) _exit(92);
    fault_count=1;
    uint32_t owner=*mmio32(OWNER_OFF);
    if(owner!=my_id-1) _exit(93);
    fault_from=owner;
    uint64_t t0=nsec_now();
    *mmio32(REQUESTER_OFF)=my_id;
    *mmio32(SEQ_OFF)=my_id;
    fence();
    *mmio32(STATE_OFF)=STATE_REQ;
    fence();
    uint64_t end=t0+3000000000ull;
    while(nsec_now()<end){
        if(*mmio32(STATE_OFF)==STATE_RESP && *mmio32(OWNER_OFF)==my_id) break;
    }
    if(*mmio32(STATE_OFF)!=STATE_RESP || *mmio32(OWNER_OFF)!=my_id) _exit(94);
    if(mprotect(fault_page,PAGE_SZ,PROT_READ|PROT_WRITE)) _exit(95);
    volatile uint8_t *src=bar2+TRANSFER_OFF;
    for(unsigned i=0;i<PAGE_SZ;i++) fault_page[i]=src[i];
    fence();
    *mmio32(STATE_OFF)=STATE_ACK;
    fence();
    fault_latency_ns=nsec_now()-t0;
}

static unsigned read_id(void){
    FILE *f=fopen("/proc/cmdline","r"); if(!f) die("cmdline");
    char buf[1024]={0}; if(!fgets(buf,sizeof(buf),f)) die("cmdline read"); fclose(f);
    char *p=strstr(buf,"rkmesh_id="); if(!p) { fprintf(stderr,"missing rkmesh_id\n"); _exit(81); }
    unsigned id=(unsigned)strtoul(p+10,NULL,10); if(id>3) _exit(82); return id;
}
static void setup_fs(void){
    mkdir("/proc",0555); mkdir("/sys",0555); mkdir("/dev",0755);
    if(mount("proc","/proc","proc",0,0) && errno!=EBUSY) die("mount proc");
    if(mount("sysfs","/sys","sysfs",0,0) && errno!=EBUSY) die("mount sys");
    if(mount("devtmpfs","/dev","devtmpfs",0,0) && errno!=EBUSY) die("mount dev");
}
static void map_bar2(void){
    const char *path="/sys/bus/pci/devices/0000:00:01.0/resource2";
    int fd=open(path,O_RDWR|O_SYNC); if(fd<0) die("open resource2");
    void *p=mmap(NULL,BAR2_LEN,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0); close(fd);
    if(p==MAP_FAILED) die("mmap resource2");
    bar2=(volatile uint8_t*)p;
}
static void init_local_page(uint8_t *p){
    for(unsigned i=0;i<PAGE_SZ;i++) p[i]=(uint8_t)((i*37u+11u)&0xffu);
    ((uint64_t*)p)[0]=0x3588358835883588ull;
}
static void serve_next(uint8_t *local,unsigned next){
    uint64_t end=nsec_now()+3000000000ull;
    while(nsec_now()<end){
        if(*mmio32(STATE_OFF)==STATE_REQ && *mmio32(REQUESTER_OFF)==next) break;
        usleep(500);
    }
    if(*mmio32(STATE_OFF)!=STATE_REQ || *mmio32(REQUESTER_OFF)!=next){ fprintf(stderr,"RKMESH_FAULT_FAIL id=%u reason=request_timeout next=%u\n",my_id,next); _exit(83); }
    volatile uint8_t *dst=bar2+TRANSFER_OFF;
    for(unsigned i=0;i<PAGE_SZ;i++) dst[i]=local[i];
    fence();
    *mmio32(OWNER_OFF)=next;
    fence();
    *mmio32(STATE_OFF)=STATE_RESP;
    fence();
    if(wait32(mmio32(STATE_OFF),STATE_ACK,3000)){ fprintf(stderr,"RKMESH_FAULT_FAIL id=%u reason=ack_timeout\n",my_id); _exit(84); }
    *mmio32(STATE_OFF)=STATE_IDLE;
    fence();
    printf("RKMESH_PAGE_TRANSFER from=%u to=%u bytes=%u checksum=0x%016llx\n",my_id,next,PAGE_SZ,(unsigned long long)checksum_page(local));
    fflush(stdout);
}
int main(void){
    setvbuf(stdout,NULL,_IONBF,0); setvbuf(stderr,NULL,_IONBF,0);
    setup_fs(); my_id=read_id(); map_bar2();
    *mmio32(READY_BASE+my_id*4)=READY_MAGIC|my_id; fence();
    printf("RKMESH_FAULT_READY id=%u bar2_mapped=1\n",my_id);
    if(my_id==0){
        for(unsigned i=0;i<4;i++) if(wait32(mmio32(READY_BASE+i*4),READY_MAGIC|i,3000)){ fprintf(stderr,"RKMESH_FAULT_FAIL id=0 reason=peer_ready_%u\n",i); _exit(85); }
        uint8_t *local=mmap(NULL,PAGE_SZ,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0); if(local==MAP_FAILED) die("mmap local");
        init_local_page(local);
        *mmio32(STATE_OFF)=STATE_IDLE; *mmio32(OWNER_OFF)=0; fence(); *mmio32(INIT_OFF)=INIT_MAGIC; fence();
        printf("RKMESH_PAGE_OWNER id=0 checksum=0x%016llx\n",(unsigned long long)checksum_page(local));
        serve_next(local,1);
    }else{
        if(wait32(mmio32(INIT_OFF),INIT_MAGIC,3000)){ fprintf(stderr,"RKMESH_FAULT_FAIL id=%u reason=init_timeout\n",my_id); _exit(86); }
        fault_page=mmap(NULL,PAGE_SZ,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0); if(fault_page==MAP_FAILED) die("mmap fault");
        struct sigaction sa; memset(&sa,0,sizeof(sa)); sa.sa_sigaction=segv_handler; sa.sa_flags=SA_SIGINFO; sigemptyset(&sa.sa_mask); if(sigaction(SIGSEGV,&sa,NULL)) die("sigaction");
        uint64_t end=nsec_now()+5000000000ull;
        while(nsec_now()<end){ if(*mmio32(OWNER_OFF)==my_id-1 && *mmio32(STATE_OFF)==STATE_IDLE) break; usleep(500); }
        if(*mmio32(OWNER_OFF)!=my_id-1 || *mmio32(STATE_OFF)!=STATE_IDLE){ fprintf(stderr,"RKMESH_FAULT_FAIL id=%u reason=ownership_wait\n",my_id); _exit(87); }
        volatile uint64_t first=((volatile uint64_t*)fault_page)[0];
        (void)first;
        if(fault_count!=1){ fprintf(stderr,"RKMESH_FAULT_FAIL id=%u reason=fault_count count=%d\n",my_id,(int)fault_count); _exit(88); }
        uint64_t before=checksum_page(fault_page);
        ((uint64_t*)fault_page)[my_id]=mix(((uint64_t*)fault_page)[my_id-1],my_id);
        uint64_t after=checksum_page(fault_page);
        printf("RKMESH_FAULT_RESUMED id=%u from=%u faults=%d latency_ns=%llu before=0x%016llx after=0x%016llx\n",my_id,fault_from,(int)fault_count,(unsigned long long)fault_latency_ns,(unsigned long long)before,(unsigned long long)after);
        if(my_id<3) serve_next(fault_page,my_id+1);
        else { *mmio32(DONE_OFF)=DONE_MAGIC; fence(); printf("RKMESH_FAULT_CHAIN_DONE owner=3 final_checksum=0x%016llx\n",(unsigned long long)after); }
    }
    if(wait32(mmio32(DONE_OFF),DONE_MAGIC,5000)){ fprintf(stderr,"RKMESH_FAULT_FAIL id=%u reason=done_timeout\n",my_id); _exit(89); }
    printf("RKMESH_FAULT_PASS id=%u owner=%u faults=%d\n",my_id,*mmio32(OWNER_OFF),(int)fault_count);
    sync(); reboot(RB_POWER_OFF); for(;;) pause();
}
