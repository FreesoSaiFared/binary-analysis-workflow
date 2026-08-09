#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <setjmp.h>
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
#define NPAGES 16u
#define LEASE_BYTES (4u*PAGE_SZ)
#define DONE_MAGIC 0x600df003U
#define MODE_READ 1U
#define MODE_WRITE 2U
#define MODE_LEASE 3U
#define MODE_STALE 9U
#define RK_IOC_MAGIC 'M'
struct rk_xfer{uint64_t user_ptr,latency_ns;uint32_t index,mode,from,irq_count;};
struct rk_status{uint32_t id,step,done,irq_count;uint32_t owner0,owner1,readers0,readers1;uint32_t invalidations,page_xfers,lease_xfers,lease_owner;uint64_t page_bytes,lease_bytes,final_checksum,lease_checksum;};
#define RK_WAIT_READY _IO(RK_IOC_MAGIC,1)
#define RK_GET_STATUS _IOR(RK_IOC_MAGIC,2,struct rk_status)
#define RK_ACQUIRE _IOWR(RK_IOC_MAGIC,3,struct rk_xfer)
#define RK_COMMIT _IOW(RK_IOC_MAGIC,4,struct rk_xfer)
#define RK_WAIT_INVALIDATE _IOR(RK_IOC_MAGIC,5,uint32_t)
#define RK_ACK_INVALIDATE _IOW(RK_IOC_MAGIC,6,uint32_t)
#define RK_WAIT_STEP _IOW(RK_IOC_MAGIC,7,uint32_t)
#define RK_SET_STEP _IOW(RK_IOC_MAGIC,8,uint32_t)
#define RK_FINALIZE _IO(RK_IOC_MAGIC,9)
#define RK_WAIT_DONE _IO(RK_IOC_MAGIC,10)
static int fd=-1;static uint8_t*region;static uint8_t*lease_buf;static unsigned my_id;static volatile sig_atomic_t desired[NPAGES],faults[NPAGES],invalidated[NPAGES],stale_faults;static sigjmp_buf stale_jmp;static volatile sig_atomic_t stale_armed;
static uint64_t checksum(const uint8_t*p,size_t n){uint64_t h=0xcbf29ce484222325ULL;size_t i;for(i=0;i<n;i++){h^=p[i];h*=0x100000001b3ULL;}return h;}
static uint64_t mix(uint64_t x,unsigned id){x^=0x9e3779b97f4a7c15ULL+id+(x<<6)+(x>>2);return x*0xbf58476d1ce4e5b9ULL;}
static void die(const char*m){perror(m);_exit(80);}
static void segv(int s,siginfo_t*si,void*ctx){uintptr_t a=(uintptr_t)si->si_addr,b=(uintptr_t)region;unsigned p;struct rk_xfer x;(void)s;(void)ctx;if(a<b||a>=b+NPAGES*PAGE_SZ)_exit(91);p=(unsigned)((a-b)/PAGE_SZ);if(desired[p]==MODE_STALE&&stale_armed){stale_faults++;stale_armed=0;dprintf(1,"RKMESH_V3_STALE_FAULT id=%u page=%u count=%d\n",my_id,p,(int)stale_faults);siglongjmp(stale_jmp,1);}if(desired[p]!=MODE_READ&&desired[p]!=MODE_WRITE){struct rk_status ds={0};ioctl(fd,RK_GET_STATUS,&ds);dprintf(1,"RKMESH_V3_UNEXPECTED_FAULT id=%u tid=%ld addr=0x%llx page=%u desired=%u step=%u invalidated=%u stale_armed=%u\n",my_id,(long)syscall(SYS_gettid),(unsigned long long)a,p,(unsigned)desired[p],ds.step,(unsigned)invalidated[p],(unsigned)stale_armed);_exit(92);}if(faults[p])_exit(93);faults[p]=1;dprintf(1,"RKMESH_V3_FAULT id=%u page=%u mode=%u\n",my_id,p,(unsigned)desired[p]);if(mprotect(region+p*PAGE_SZ,PAGE_SZ,PROT_READ|PROT_WRITE))_exit(94);memset(&x,0,sizeof(x));x.user_ptr=(uintptr_t)(region+p*PAGE_SZ);x.index=p;x.mode=desired[p];if(ioctl(fd,RK_ACQUIRE,&x))_exit(95);if(desired[p]==MODE_READ&&mprotect(region+p*PAGE_SZ,PAGE_SZ,PROT_READ))_exit(96);dprintf(1,"RKMESH_V3_FAULT_RESUMED id=%u page=%u mode=%u from=%u latency_ns=%llu\n",my_id,p,x.mode,x.from,(unsigned long long)x.latency_ns);}
static void*inv_thread(void*arg){(void)arg;for(;;){uint32_t p;if(ioctl(fd,RK_WAIT_INVALIDATE,&p))continue;if(p>=NPAGES)_exit(97);if(mprotect(region+p*PAGE_SZ,PAGE_SZ,PROT_NONE))_exit(98);invalidated[p]=1;dprintf(1,"RKMESH_V3_INVALIDATED id=%u page=%u\n",my_id,p);if(ioctl(fd,RK_ACK_INVALIDATE,&p))_exit(99);}return NULL;}
static void setup_fs(void){mkdir("/proc",0555);mkdir("/sys",0555);mkdir("/dev",0755);if(mount("proc","/proc","proc",0,0)&&errno!=EBUSY)die("proc");if(mount("sysfs","/sys","sysfs",0,0)&&errno!=EBUSY)die("sys");if(mount("devtmpfs","/dev","devtmpfs",0,0)&&errno!=EBUSY)die("dev");}
static void loadmod(void){int f=open("/rkmesh_pages.ko",O_RDONLY),rc,e;if(f<0)die("module");rc=syscall(SYS_finit_module,f,"",0);e=errno;close(f);printf("RKMESH_V3_MODULE_LOAD rc=%d errno=%d\n",rc,e);if(rc)_exit(81);}
static int opendev(void){int i,x;for(i=0;i<1000;i++){x=open("/dev/rkmesh_pages",O_RDWR);if(x>=0)return x;usleep(1000);}return -1;}
static void wait_step(uint32_t s){if(ioctl(fd,RK_WAIT_STEP,&s))die("wait step");}
static void set_step(uint32_t s){if(ioctl(fd,RK_SET_STEP,&s))die("set step");}
static inline void compiler_barrier(void){__asm__ __volatile__("" ::: "memory");}
static void touch_read(unsigned p){desired[p]=MODE_READ;compiler_barrier();(void)((volatile uint64_t*)(region+p*PAGE_SZ))[0];compiler_barrier();}
static void touch_write(unsigned p){desired[p]=MODE_WRITE;compiler_barrier();(void)((volatile uint64_t*)(region+p*PAGE_SZ))[0];compiler_barrier();}
static void commit_page(unsigned p){struct rk_xfer x={0};x.user_ptr=(uintptr_t)(region+p*PAGE_SZ);x.index=p;x.mode=MODE_WRITE;if(ioctl(fd,RK_COMMIT,&x))die("commit page");}
static void stale_probe(unsigned p){desired[p]=MODE_STALE;stale_armed=1;if(sigsetjmp(stale_jmp,1)==0){volatile uint64_t v=((volatile uint64_t*)(region+p*PAGE_SZ))[0];(void)v;_exit(110);}if(!invalidated[p]||!stale_faults)_exit(111);}
static void lease_acquire_and_commit(unsigned expected_from){struct rk_xfer x={0};x.user_ptr=(uintptr_t)lease_buf;x.index=0xffff;x.mode=MODE_LEASE;if(ioctl(fd,RK_ACQUIRE,&x))die("lease acquire");if(x.from!=expected_from)_exit(112);((uint64_t*)lease_buf)[my_id]=mix(((uint64_t*)lease_buf)[my_id-1],my_id+10);if(ioctl(fd,RK_COMMIT,&x))die("lease commit");printf("RKMESH_V3_LEASE_ACQUIRED id=%u from=%u latency_ns=%llu checksum=0x%016llx\n",my_id,x.from,(unsigned long long)x.latency_ns,(unsigned long long)checksum(lease_buf,LEASE_BYTES));}
int main(void){struct sigaction sa;struct rk_status st;pthread_t th;setvbuf(stdout,NULL,_IONBF,0);setvbuf(stderr,NULL,_IONBF,0);setup_fs();loadmod();fd=opendev();if(fd<0)die("open");if(ioctl(fd,RK_WAIT_READY))die("ready");if(ioctl(fd,RK_GET_STATUS,&st))die("status");my_id=st.id;region=mmap(NULL,NPAGES*PAGE_SZ,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);lease_buf=mmap(NULL,LEASE_BYTES,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);if(region==MAP_FAILED||lease_buf==MAP_FAILED)die("mmap");memset(&sa,0,sizeof(sa));sa.sa_sigaction=segv;sa.sa_flags=SA_SIGINFO;sigemptyset(&sa.sa_mask);if(sigaction(SIGSEGV,&sa,NULL))die("sigaction");if(pthread_create(&th,NULL,inv_thread,NULL))die("pthread");printf("RKMESH_V3_USER_READY id=%u owner0=%u owner1=%u\n",my_id,st.owner0,st.owner1);
if(my_id==1){wait_step(0);touch_read(0);set_step(1);wait_step(2);stale_probe(0);set_step(3);wait_step(6);lease_acquire_and_commit(0);set_step(7);}else if(my_id==2){wait_step(1);touch_write(0);{volatile uint64_t*p0=(volatile uint64_t*)region;p0[2]=mix(p0[1],2);}commit_page(0);set_step(2);wait_step(7);lease_acquire_and_commit(1);set_step(8);}else if(my_id==3){wait_step(3);touch_read(1);set_step(4);wait_step(5);stale_probe(1);set_step(6);wait_step(8);lease_acquire_and_commit(2);set_step(9);if(ioctl(fd,RK_FINALIZE))die("finalize");}else{wait_step(4);touch_write(1);((uint64_t*)(region+PAGE_SZ))[0]^=0x0f0e0d0c0b0a0908ULL;commit_page(1);set_step(5);}
if(ioctl(fd,RK_WAIT_DONE)) die("done");
if(ioctl(fd,RK_GET_STATUS,&st)) die("final status");
if(st.done!=DONE_MAGIC||st.owner0!=2||st.owner1!=0||st.readers0!=0||st.readers1!=0||st.invalidations!=2||st.page_xfers!=4||st.lease_xfers!=3||st.lease_owner!=3||st.page_bytes!=16384||st.lease_bytes!=49152) _exit(120);
printf("RKMESH_V3_PASS id=%u irq_count=%u owner0=%u owner1=%u invalidations=%u page_xfers=%u page_bytes=%llu lease_xfers=%u lease_bytes=%llu final=0x%016llx\n",my_id,st.irq_count,st.owner0,st.owner1,st.invalidations,st.page_xfers,(unsigned long long)st.page_bytes,st.lease_xfers,(unsigned long long)st.lease_bytes,(unsigned long long)st.final_checksum);
sync(); reboot(RB_POWER_OFF); for(;;) pause();
}
