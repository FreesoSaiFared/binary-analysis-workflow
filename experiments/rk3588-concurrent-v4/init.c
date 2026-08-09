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
#define DONE_MAGIC 0x600df004U
#define MODE_READ 1U
#define MODE_WRITE 2U
#define MODE_LEASE 3U
#define MODE_STALE 9U
#define RK_IOC_MAGIC 'N'
struct rk_xfer{uint64_t user_ptr,latency_ns;uint32_t index,mode,from,irq_count;uint32_t gate,delay_ms,retries,generation;};
struct rk_inv{uint32_t slot,page,generation,reserved;};
struct rk_gate{uint32_t gate,slot_mask;};
struct rk_phase{uint32_t phase,node_mask;};
struct rk_status{uint32_t id,step,done,irq_count;uint32_t owner0,owner2,owner4;uint32_t readers0,readers2;uint32_t busy0,busy2,busy4;uint32_t overlaps,delay_proof,lease_owner,lease_busy;uint32_t page_xfers,lease_xfers,invalidations,slot2_retries;uint64_t page_bytes,lease_bytes,final_checksum,lease_checksum;};
#define RK_WAIT_READY _IO(RK_IOC_MAGIC,1)
#define RK_GET_STATUS _IOR(RK_IOC_MAGIC,2,struct rk_status)
#define RK_ACQUIRE _IOWR(RK_IOC_MAGIC,3,struct rk_xfer)
#define RK_COMMIT _IOW(RK_IOC_MAGIC,4,struct rk_xfer)
#define RK_WAIT_INVALIDATE _IOR(RK_IOC_MAGIC,5,struct rk_inv)
#define RK_ACK_INVALIDATE _IOW(RK_IOC_MAGIC,6,struct rk_inv)
#define RK_WAIT_STEP _IOW(RK_IOC_MAGIC,7,uint32_t)
#define RK_SET_STEP _IOW(RK_IOC_MAGIC,8,uint32_t)
#define RK_GATE_RELEASE _IOW(RK_IOC_MAGIC,9,struct rk_gate)
#define RK_ASSERT_DELAY _IO(RK_IOC_MAGIC,10)
#define RK_MARK_PHASE _IOW(RK_IOC_MAGIC,11,uint32_t)
#define RK_WAIT_PHASE _IOW(RK_IOC_MAGIC,12,struct rk_phase)
#define RK_FINALIZE _IO(RK_IOC_MAGIC,13)
#define RK_WAIT_DONE _IO(RK_IOC_MAGIC,14)
static int fd=-1;static uint8_t*region,*lease_buf;static unsigned my_id;static volatile sig_atomic_t desired[NPAGES],faults[NPAGES],invalidated[NPAGES],desired_gate[NPAGES],desired_delay[NPAGES],stale_faults;static sigjmp_buf stale_jmp;static volatile sig_atomic_t stale_armed;
static uint64_t checksum(const uint8_t*p,size_t n){uint64_t h=0xcbf29ce484222325ULL;size_t i;for(i=0;i<n;i++){h^=p[i];h*=0x100000001b3ULL;}return h;}
static uint64_t mix(uint64_t x,unsigned id){x^=0x9e3779b97f4a7c15ULL+id+(x<<6)+(x>>2);return x*0xbf58476d1ce4e5b9ULL;}
static uint64_t initial_word0(unsigned pg){return 0x3588358835883588ULL^((uint64_t)pg*0x0101010101010101ULL);}
static void die(const char*m){perror(m);_exit(80);}
static inline void compiler_barrier(void){__asm__ __volatile__("":::"memory");}
static void segv(int s,siginfo_t*si,void*ctx){uintptr_t a=(uintptr_t)si->si_addr,b=(uintptr_t)region;unsigned p;struct rk_xfer x;(void)s;(void)ctx;if(a<b||a>=b+NPAGES*PAGE_SZ)_exit(91);p=(unsigned)((a-b)/PAGE_SZ);if(desired[p]==MODE_STALE&&stale_armed){stale_faults++;stale_armed=0;dprintf(1,"RKMESH_V4_STALE_FAULT id=%u page=%u count=%d\n",my_id,p,(int)stale_faults);siglongjmp(stale_jmp,1);}if(desired[p]!=MODE_READ&&desired[p]!=MODE_WRITE){dprintf(1,"RKMESH_V4_UNEXPECTED_FAULT id=%u addr=0x%llx page=%u desired=%u\n",my_id,(unsigned long long)a,p,(unsigned)desired[p]);_exit(92);}if(faults[p])_exit(93);faults[p]=1;dprintf(1,"RKMESH_V4_FAULT id=%u page=%u mode=%u gate=%u delay=%u\n",my_id,p,(unsigned)desired[p],(unsigned)desired_gate[p],(unsigned)desired_delay[p]);if(mprotect(region+p*PAGE_SZ,PAGE_SZ,PROT_READ|PROT_WRITE))_exit(94);memset(&x,0,sizeof(x));x.user_ptr=(uintptr_t)(region+p*PAGE_SZ);x.index=p;x.mode=desired[p];x.gate=desired_gate[p];x.delay_ms=desired_delay[p];if(ioctl(fd,RK_ACQUIRE,&x))_exit(95);if(desired[p]==MODE_READ&&mprotect(region+p*PAGE_SZ,PAGE_SZ,PROT_READ))_exit(96);dprintf(1,"RKMESH_V4_FAULT_RESUMED id=%u page=%u mode=%u from=%u latency_ns=%llu retries=%u gen=%u\n",my_id,p,x.mode,x.from,(unsigned long long)x.latency_ns,x.retries,x.generation);}
static void*inv_thread(void*arg){(void)arg;for(;;){struct rk_inv v;if(ioctl(fd,RK_WAIT_INVALIDATE,&v))continue;if(v.page>=NPAGES)_exit(97);if(mprotect(region+v.page*PAGE_SZ,PAGE_SZ,PROT_NONE))_exit(98);invalidated[v.page]=1;dprintf(1,"RKMESH_V4_INVALIDATED id=%u slot=%u page=%u gen=%u\n",my_id,v.slot,v.page,v.generation);if(ioctl(fd,RK_ACK_INVALIDATE,&v))_exit(99);}return NULL;}
static void setup_fs(void){mkdir("/proc",0555);mkdir("/sys",0555);mkdir("/dev",0755);if(mount("proc","/proc","proc",0,0)&&errno!=EBUSY)die("proc");if(mount("sysfs","/sys","sysfs",0,0)&&errno!=EBUSY)die("sys");if(mount("devtmpfs","/dev","devtmpfs",0,0)&&errno!=EBUSY)die("dev");}
static void loadmod(void){int f=open("/rkmesh_concurrent.ko",O_RDONLY),rc,e;if(f<0)die("module");rc=syscall(SYS_finit_module,f,"",0);e=errno;close(f);printf("RKMESH_V4_MODULE_LOAD rc=%d errno=%d\n",rc,e);if(rc)_exit(81);}
static int opendev(void){int i,x;for(i=0;i<1000;i++){x=open("/dev/rkmesh_concurrent",O_RDWR);if(x>=0)return x;usleep(1000);}return -1;}
static void wait_step(uint32_t s){if(ioctl(fd,RK_WAIT_STEP,&s))die("wait step");}
static void set_step(uint32_t s){if(ioctl(fd,RK_SET_STEP,&s))die("set step");}
static void mark_phase(uint32_t p){if(ioctl(fd,RK_MARK_PHASE,&p))die("mark phase");}
static void wait_phase(uint32_t p,uint32_t mask){struct rk_phase x={p,mask};if(ioctl(fd,RK_WAIT_PHASE,&x))die("wait phase");}
static void release_gate(uint32_t g,uint32_t mask){struct rk_gate x={g,mask};if(ioctl(fd,RK_GATE_RELEASE,&x))die("gate release");}
static void touch_read(unsigned p,unsigned gate,unsigned delay){desired[p]=MODE_READ;desired_gate[p]=gate;desired_delay[p]=delay;compiler_barrier();(void)((volatile uint64_t*)(region+p*PAGE_SZ))[0];compiler_barrier();}
static void touch_write(unsigned p,unsigned gate,unsigned delay){desired[p]=MODE_WRITE;desired_gate[p]=gate;desired_delay[p]=delay;compiler_barrier();(void)((volatile uint64_t*)(region+p*PAGE_SZ))[0];compiler_barrier();}
static void commit_page(unsigned p){struct rk_xfer x={0};x.user_ptr=(uintptr_t)(region+p*PAGE_SZ);x.index=p;x.mode=MODE_WRITE;if(ioctl(fd,RK_COMMIT,&x))die("commit page");}
static void stale_probe(unsigned p){desired[p]=MODE_STALE;compiler_barrier();stale_armed=1;if(sigsetjmp(stale_jmp,1)==0){volatile uint64_t v=((volatile uint64_t*)(region+p*PAGE_SZ))[0];(void)v;_exit(110);}if(!invalidated[p]||!stale_faults)_exit(111);}
static void wait_invalidated(unsigned p){int i;for(i=0;i<6000&&!invalidated[p];i++)usleep(1000);if(!invalidated[p])_exit(112);}
static void lease_acquire_commit(unsigned expected_from){struct rk_xfer x={0};x.user_ptr=(uintptr_t)lease_buf;x.index=0xffff;x.mode=MODE_LEASE;if(ioctl(fd,RK_ACQUIRE,&x))die("lease acquire");if(x.from!=expected_from)_exit(113);((uint64_t*)lease_buf)[3]=mix(((uint64_t*)lease_buf)[2],63);if(ioctl(fd,RK_COMMIT,&x))die("lease commit");printf("RKMESH_V4_LEASE_ACQUIRED id=%u from=%u retries=%u checksum=0x%016llx\n",my_id,x.from,x.retries,(unsigned long long)checksum(lease_buf,LEASE_BYTES));}
int main(void){struct sigaction sa;struct rk_status st;pthread_t th;volatile uint64_t*p0,*p2,*p4;uint64_t expected;setvbuf(stdout,NULL,_IONBF,0);setvbuf(stderr,NULL,_IONBF,0);setup_fs();loadmod();fd=opendev();if(fd<0)die("open");if(ioctl(fd,RK_WAIT_READY))die("ready");if(ioctl(fd,RK_GET_STATUS,&st))die("status");my_id=st.id;region=mmap(NULL,NPAGES*PAGE_SZ,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);lease_buf=mmap(NULL,LEASE_BYTES,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);if(region==MAP_FAILED||lease_buf==MAP_FAILED)die("mmap");memset(&sa,0,sizeof(sa));sa.sa_sigaction=segv;sa.sa_flags=SA_SIGINFO;sigemptyset(&sa.sa_mask);if(sigaction(SIGSEGV,&sa,NULL))die("sigaction");if(pthread_create(&th,NULL,inv_thread,NULL))die("pthread");printf("RKMESH_V4_USER_READY id=%u owner0=%u owner2=%u owner4=%u\n",my_id,st.owner0,st.owner2,st.owner4);
/* Phase 0: concurrent reads. Slot 1 is deliberately delayed; slot 3 must finish independently. */
if(my_id==1){wait_step(0);touch_read(0,1,30);mark_phase(1);}else if(my_id==3){wait_step(0);touch_read(2,1,0);mark_phase(1);}else if(my_id==0){wait_step(0);release_gate(1,(1U<<1)|(1U<<3));if(ioctl(fd,RK_ASSERT_DELAY))die("delay isolation");wait_phase(1,(1U<<1)|(1U<<3));set_step(1);}else wait_step(1);
/* Phase 1: two independent writes overlap and revoke different stale readers. */
if(my_id==0){touch_write(2,2,0);p2=(volatile uint64_t*)(region+2*PAGE_SZ);p2[0]^=0x0f0e0d0c0b0a0908ULL;commit_page(2);mark_phase(2);wait_step(2);}else if(my_id==2){wait_step(1);touch_write(0,2,0);p0=(volatile uint64_t*)region;p0[2]=mix(p0[1],20);commit_page(0);mark_phase(2);wait_step(2);}else if(my_id==1){wait_step(1);release_gate(2,(1U<<0)|(1U<<2));wait_invalidated(0);stale_probe(0);mark_phase(2);wait_phase(2,(1U<<0)|(1U<<2)|(1U<<3));set_step(2);}else if(my_id==3){wait_step(1);wait_invalidated(2);stale_probe(2);mark_phase(2);wait_step(2);}
/* Phase 2: two writers collide on page 4. Slot priority makes id1 win first; id2 must retry until id1 commits. */
if(my_id==0){release_gate(3,(1U<<1)|(1U<<2));wait_phase(3,(1U<<1)|(1U<<2));set_step(3);}else if(my_id==1){touch_write(4,3,0);p4=(volatile uint64_t*)(region+4*PAGE_SZ);p4[0]=mix(p4[0],41);commit_page(4);mark_phase(3);wait_step(3);}else if(my_id==2){touch_write(4,3,0);p4=(volatile uint64_t*)(region+4*PAGE_SZ);expected=mix(initial_word0(4),41);if(p4[0]!=expected){dprintf(1,"RKMESH_V4_STALE_COLLISION_DATA id=2 got=0x%016llx expected=0x%016llx\n",(unsigned long long)p4[0],(unsigned long long)expected);_exit(114);}p4[1]=mix(p4[0],42);commit_page(4);mark_phase(3);wait_step(3);}else wait_step(3);
/* Phase 3: keep the larger object-lease path alive as a regression. */
if(my_id==3){lease_acquire_commit(0);mark_phase(4);}else if(my_id==0){wait_phase(4,1U<<3);if(ioctl(fd,RK_FINALIZE))die("finalize");}
if(ioctl(fd,RK_WAIT_DONE))die("wait done");
if(ioctl(fd,RK_GET_STATUS,&st))die("final status");
if(st.done!=DONE_MAGIC||st.owner0!=2||st.owner2!=0||st.owner4!=2||st.readers0!=0||st.readers2!=0||st.busy0||st.busy2||st.busy4||st.overlaps!=3||st.delay_proof!=1||st.lease_owner!=3||st.lease_busy||st.page_xfers!=6||st.page_bytes!=24576||st.invalidations!=2||st.lease_xfers!=1||st.lease_bytes!=16384||st.slot2_retries<1)_exit(120);
printf("RKMESH_V4_PASS id=%u irq_count=%u owner0=%u owner2=%u owner4=%u overlaps=%u delay=%u invalidations=%u page_xfers=%u page_bytes=%llu retries_slot2=%u lease_xfers=%u lease_bytes=%llu final=0x%016llx\n",my_id,st.irq_count,st.owner0,st.owner2,st.owner4,st.overlaps,st.delay_proof,st.invalidations,st.page_xfers,(unsigned long long)st.page_bytes,st.slot2_retries,st.lease_xfers,(unsigned long long)st.lease_bytes,(unsigned long long)st.final_checksum);sync();reboot(RB_POWER_OFF);for(;;)pause();}
