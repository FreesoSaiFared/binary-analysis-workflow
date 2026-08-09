#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define PAGE_SZ 4096u
#define NPAGES 16u
#define NODES 4u
#define MODE_READ 1u
#define MODE_WRITE 2u
#define DONE_MAGIC 0x600df007U
#define AUTO_CANDIDATE 4u
#define TASK_STATE_BYTES 8192u
#define EXPECTED_TASK_CHECKSUM 0x4a9f55cc4def6f8dULL
#define RK_IOC_MAGIC 'P'

struct rk_xfer{uint64_t user_ptr,latency_ns;uint32_t index,mode,from,irq_count;uint32_t gate,delay_ms,retries,generation;};
struct rk_status{
 uint32_t id,step,done,irq_count;
 uint32_t owner0,owner1,owner2,owner4,owner5,owner6,owner9;
 uint32_t readers0,readers2;
 uint32_t busy0,busy2,busy4,busy6;
 uint32_t overlaps,delay_proof,lease_owner,lease_busy;
 uint32_t page_xfers,lease_xfers,invalidations,slot2_retries;
 uint32_t local_hits0,local_hits1,local_hits2,local_hits3;
 uint32_t task_target,task_scenario;
 uint32_t lock_owner,lock_wait_mask,cs_owner;
 uint32_t lock_acquires,lock_waits,lock_wakes,lock_releases;
 uint32_t stale_wakes,protected_rejects,cs_violations;
 uint64_t page_bytes,lease_bytes,final_checksum,lease_checksum,protected_checksum;
};

#define RK_WAIT_READY _IO(RK_IOC_MAGIC,1)
#define RK_GET_STATUS _IOR(RK_IOC_MAGIC,2,struct rk_status)
#define RK_ACQUIRE _IOWR(RK_IOC_MAGIC,3,struct rk_xfer)
#define RK_COMMIT _IOW(RK_IOC_MAGIC,4,struct rk_xfer)
#define RK_WAIT_STEP _IOW(RK_IOC_MAGIC,7,uint32_t)
#define RK_SET_STEP _IOW(RK_IOC_MAGIC,8,uint32_t)
#define RK_WAIT_DONE _IO(RK_IOC_MAGIC,14)
#define RK_SET_TARGET _IOW(RK_IOC_MAGIC,20,uint32_t)
#define RK_SET_SCENARIO _IOW(RK_IOC_MAGIC,21,uint32_t)
#define RK_TASK_FINISH _IOW(RK_IOC_MAGIC,22,uint64_t)

static int fd=-1;
static unsigned my_id;

static void die(const char*m){perror(m);_exit(80);}
static void setup_fs(void){mkdir("/proc",0555);mkdir("/sys",0555);mkdir("/dev",0755);if(mount("proc","/proc","proc",0,0)&&errno!=EBUSY)die("proc");if(mount("sysfs","/sys","sysfs",0,0)&&errno!=EBUSY)die("sys");if(mount("devtmpfs","/dev","devtmpfs",0,0)&&errno!=EBUSY)die("dev");}
static void loadmod(void){int f=open("/rkmesh_v7.ko",O_RDONLY),rc,e;if(f<0)die("module");rc=syscall(SYS_finit_module,f,"",0);e=errno;close(f);printf("RKMESH_V7_MODULE_LOAD rc=%d errno=%d\n",rc,e);if(rc)_exit(81);}
static int opendev(void){int i,x;for(i=0;i<1000;i++){x=open("/dev/rkmesh_v7",O_RDWR);if(x>=0)return x;usleep(1000);}return -1;}
static void wait_step(uint32_t s){if(ioctl(fd,RK_WAIT_STEP,&s))die("wait step");}
static void set_step(uint32_t s){if(ioctl(fd,RK_SET_STEP,&s))die("set step");}
static void get_status(struct rk_status*s){if(ioctl(fd,RK_GET_STATUS,s))die("status");}

static unsigned cmd_u(const char*key,unsigned def){
 char buf[2048],*p,*e;int f=open("/proc/cmdline",O_RDONLY);ssize_t n;if(f<0)return def;n=read(f,buf,sizeof(buf)-1);close(f);if(n<=0)return def;buf[n]=0;p=strstr(buf,key);if(!p)return def;p+=strlen(key);if(*p!='=')return def;p++;return (unsigned)strtoul(p,&e,10);
}

static uint64_t fnv_update(uint64_t h,const uint8_t*p,size_t n){size_t i;for(i=0;i<n;i++){h^=p[i];h*=0x100000001b3ULL;}return h;}

static void acquire_page(unsigned page,unsigned mode,uint8_t*buf){
 struct rk_xfer x;memset(&x,0,sizeof(x));x.user_ptr=(uintptr_t)buf;x.index=page;x.mode=mode;
 if(ioctl(fd,RK_ACQUIRE,&x))die(mode==MODE_READ?"read acquire":"write acquire");
 printf("RKMESH_V7_USER_ACQUIRE id=%u page=%u mode=%u from=%u latency_ns=%llu retries=%u\n",
        my_id,page,mode,x.from,(unsigned long long)x.latency_ns,x.retries);
}
static void commit_page(unsigned page,uint8_t*buf){struct rk_xfer x;memset(&x,0,sizeof(x));x.user_ptr=(uintptr_t)buf;x.index=page;x.mode=MODE_WRITE;if(ioctl(fd,RK_COMMIT,&x))die("commit");}

static unsigned owner_for(const struct rk_status*s,unsigned page){
 switch(page){case 1:return s->owner1;case 2:return s->owner2;case 5:return s->owner5;case 9:return s->owner9;default:_exit(82);}
}
static uint32_t local_hits_for(const struct rk_status*s,unsigned id){
 switch(id){case 0:return s->local_hits0;case 1:return s->local_hits1;case 2:return s->local_hits2;case 3:return s->local_hits3;default:return 0;}
}

static unsigned calculate_costs(const struct rk_status*s,uint32_t cost[4]){
 static const unsigned pages[4]={1,5,9,2};unsigned g,j,best=0;uint32_t bestc=0xffffffffu;
 for(g=0;g<4;g++){uint32_t remote=0;for(j=0;j<4;j++)if(owner_for(s,pages[j])!=g)remote+=PAGE_SZ;cost[g]=remote+(g==0?0:TASK_STATE_BYTES);if(cost[g]<bestc){bestc=cost[g];best=g;}}
 return best;
}

static void mutate_to_guest3(void){
 static const unsigned pages[3]={1,5,9};unsigned i;uint8_t*buf=aligned_alloc(PAGE_SZ,PAGE_SZ);if(!buf)die("alloc mutation");
 for(i=0;i<3;i++){acquire_page(pages[i],MODE_WRITE,buf);commit_page(pages[i],buf);printf("RKMESH_V7_MUTATE_OWNER page=%u new_owner=3\n",pages[i]);}
 free(buf);
}

static uint64_t run_task(const struct rk_status*before,uint32_t*remote_xfers,uint64_t*remote_bytes,uint32_t*local_hits){
 static const unsigned pages[4]={1,5,9,2};unsigned i;uint8_t*buf=aligned_alloc(PAGE_SZ,PAGE_SZ);struct rk_status after;uint64_t h=0xcbf29ce484222325ULL;if(!buf)die("alloc task");
 for(i=0;i<4;i++){acquire_page(pages[i],MODE_READ,buf);h=fnv_update(h,buf,PAGE_SZ);}
 get_status(&after);free(buf);
 *remote_xfers=after.page_xfers-before->page_xfers;
 *remote_bytes=after.page_bytes-before->page_bytes;
 *local_hits=local_hits_for(&after,my_id)-local_hits_for(before,my_id);
 return h;
}

int main(void){
 struct rk_status st,before;uint32_t costs[4],target,scenario,candidate,remote_xfers,local_hits,expected_remote=0,j;uint64_t remote_bytes,h;static const unsigned pages[4]={1,5,9,2};
 setvbuf(stdout,NULL,_IONBF,0);setvbuf(stderr,NULL,_IONBF,0);setup_fs();loadmod();fd=opendev();if(fd<0)die("open");if(ioctl(fd,RK_WAIT_READY))die("ready");get_status(&st);my_id=st.id;
 scenario=cmd_u("v7_scenario",0);candidate=cmd_u("v7_candidate",AUTO_CANDIDATE);if(scenario>1||candidate>AUTO_CANDIDATE)_exit(83);
 printf("RKMESH_V7_USER_READY id=%u scenario=%u candidate=%u owner1=%u owner5=%u owner9=%u owner2=%u\n",my_id,scenario,candidate,st.owner1,st.owner5,st.owner9,st.owner2);
 if(my_id==0){if(ioctl(fd,RK_SET_SCENARIO,&scenario))die("set scenario");}
 if(scenario==1){if(my_id==3){mutate_to_guest3();set_step(1);}else wait_step(1);}else{if(my_id==0)set_step(1);else wait_step(1);}
 get_status(&st);
 if(scenario==0){if(st.owner1!=1||st.owner5!=1||st.owner9!=1||st.owner2!=2)_exit(90);}
 else{if(st.owner1!=3||st.owner5!=3||st.owner9!=3||st.owner2!=2)_exit(91);}
 target=calculate_costs(&st,costs);
 if(my_id==0){
   printf("RKMESH_V7_COST_TABLE scenario=%u c0=%u c1=%u c2=%u c3=%u predicted=%u owners=%u,%u,%u,%u task_state=%u\n",
          scenario,costs[0],costs[1],costs[2],costs[3],target,st.owner1,st.owner5,st.owner9,st.owner2,TASK_STATE_BYTES);
   if((scenario==0&&target!=1)||(scenario==1&&target!=3))_exit(92);
   if(candidate!=AUTO_CANDIDATE)target=candidate;
   if(ioctl(fd,RK_SET_TARGET,&target))die("set target");
   set_step(2);
 } else wait_step(2);
 get_status(&st);target=st.task_target?st.task_target-1:99;if(target>=4)_exit(93);before=st;
 if(my_id==target){
   h=run_task(&before,&remote_xfers,&remote_bytes,&local_hits);
   for(j=0;j<4;j++)if(owner_for(&before,pages[j])!=my_id)expected_remote++;
   if(remote_xfers!=expected_remote||remote_bytes!=(uint64_t)expected_remote*PAGE_SZ||local_hits!=4-expected_remote)_exit(94);
   if(h!=EXPECTED_TASK_CHECKSUM)_exit(95);
   printf("RKMESH_V7_TASK_RESULT scenario=%u target=%u remote_xfers=%u remote_bytes=%llu local_hits=%u modeled_state_bytes=%u modeled_total=%llu checksum=0x%016llx\n",
          scenario,target,remote_xfers,(unsigned long long)remote_bytes,local_hits,target==0?0:TASK_STATE_BYTES,
          (unsigned long long)(remote_bytes+(target==0?0:TASK_STATE_BYTES)),(unsigned long long)h);
   if(ioctl(fd,RK_TASK_FINISH,&h))die("task finish");
 }
 if(ioctl(fd,RK_WAIT_DONE))die("wait done");
 get_status(&st);
 if(st.done!=DONE_MAGIC||st.final_checksum!=EXPECTED_TASK_CHECKSUM)_exit(96);
 printf("RKMESH_V7_PASS id=%u scenario=%u target=%u final=0x%016llx irq=%u\n",my_id,scenario,target,(unsigned long long)st.final_checksum,st.irq_count);
 sync();reboot(RB_POWER_OFF);for(;;)pause();
}
