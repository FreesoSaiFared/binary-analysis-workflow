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
#define NODES 4u
#define MODE_READ 1u
#define MODE_WRITE 2u
#define MOVE_COST 8192u
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
#define RK_SET_TARGET _IOW(RK_IOC_MAGIC,20,uint32_t)

static int fd=-1;
static unsigned my_id;
static const unsigned task_pages[4]={1,5,9,2};

enum { NEG_HYST=0, NEG_GREEDY=1, POS_HYST=2, POS_STAY=3 };

static void die(const char*m){perror(m);_exit(80);}
static void setup_fs(void){mkdir("/proc",0555);mkdir("/sys",0555);mkdir("/dev",0755);if(mount("proc","/proc","proc",0,0)&&errno!=EBUSY)die("proc");if(mount("sysfs","/sys","sysfs",0,0)&&errno!=EBUSY)die("sys");if(mount("devtmpfs","/dev","devtmpfs",0,0)&&errno!=EBUSY)die("dev");}
static void loadmod(void){int f=open("/rkmesh_v8.ko",O_RDONLY),rc,e;if(f<0)die("module");rc=syscall(SYS_finit_module,f,"",0);e=errno;close(f);printf("RKMESH_V8_MODULE_LOAD rc=%d errno=%d\n",rc,e);if(rc)_exit(81);}
static int opendev(void){int i,x;for(i=0;i<1000;i++){x=open("/dev/rkmesh_v8",O_RDWR);if(x>=0)return x;usleep(1000);}return -1;}
static void wait_step(uint32_t s){if(ioctl(fd,RK_WAIT_STEP,&s))die("wait step");}
static void set_step(uint32_t s){if(ioctl(fd,RK_SET_STEP,&s))die("set step");}
static void get_status(struct rk_status*s){if(ioctl(fd,RK_GET_STATUS,s))die("status");}
static unsigned cmd_u(const char*key,unsigned def){char buf[2048],*p,*e;int f=open("/proc/cmdline",O_RDONLY);ssize_t n;if(f<0)return def;n=read(f,buf,sizeof(buf)-1);close(f);if(n<=0)return def;buf[n]=0;p=strstr(buf,key);if(!p)return def;p+=strlen(key);if(*p!='=')return def;p++;return (unsigned)strtoul(p,&e,10);}
static uint64_t fnv_update(uint64_t h,const uint8_t*p,size_t n){size_t i;for(i=0;i<n;i++){h^=p[i];h*=0x100000001b3ULL;}return h;}
static uint32_t local_hits_sum(const struct rk_status*s){return s->local_hits0+s->local_hits1+s->local_hits2+s->local_hits3;}

static unsigned owner_for(const struct rk_status*s,unsigned page){switch(page){case 1:return s->owner1;case 2:return s->owner2;case 5:return s->owner5;case 9:return s->owner9;default:_exit(82);}}
static uint32_t remote_cost(const struct rk_status*s,unsigned guest){unsigned j;uint32_t c=0;for(j=0;j<4;j++)if(owner_for(s,task_pages[j])!=guest)c+=PAGE_SZ;return c;}
static unsigned best_guest(const struct rk_status*s,uint32_t costs[4]){unsigned g,best=0;uint32_t v=0xffffffffu;for(g=0;g<4;g++){costs[g]=remote_cost(s,g);if(costs[g]<v){v=costs[g];best=g;}}return best;}

static void acquire_page(unsigned page,unsigned mode,uint8_t*buf){struct rk_xfer x;memset(&x,0,sizeof(x));x.user_ptr=(uintptr_t)buf;x.index=page;x.mode=mode;if(ioctl(fd,RK_ACQUIRE,&x))die(mode==MODE_READ?"read acquire":"write acquire");printf("RKMESH_V8_ACQUIRE id=%u page=%u mode=%u from=%u latency_ns=%llu retries=%u\n",my_id,page,mode,x.from,(unsigned long long)x.latency_ns,x.retries);}
static void commit_page(unsigned page,uint8_t*buf){struct rk_xfer x;memset(&x,0,sizeof(x));x.user_ptr=(uintptr_t)buf;x.index=page;x.mode=MODE_WRITE;if(ioctl(fd,RK_COMMIT,&x))die("commit");}

static void mutate_pages(const unsigned*pages,unsigned n,unsigned config,unsigned iter){
 unsigned i;uint8_t*buf=aligned_alloc(PAGE_SZ,PAGE_SZ);struct rk_status a,b;if(!buf)die("mut alloc");get_status(&a);
 for(i=0;i<n;i++){acquire_page(pages[i],MODE_WRITE,buf);commit_page(pages[i],buf);}
 get_status(&b);printf("RKMESH_V8_MUTATION mode=%u iter=%u mutator=%u config=%c pages=%u page_xfers=%u page_bytes=%llu\n",cmd_u("v8_mode",99),iter,my_id,config?'B':'A',n,b.page_xfers-a.page_xfers,(unsigned long long)(b.page_bytes-a.page_bytes));free(buf);
}

static void perform_mutation(unsigned config,int prev,unsigned iter){
 static const unsigned p1[1]={1};static const unsigned p59[2]={5,9};
 if(prev<0){if(my_id==0){mutate_pages(p1,1,config,iter);set_step(100+iter*10+1);}return;}
 if((unsigned)prev==config){if(my_id==0)set_step(100+iter*10+1);return;}
 if(config==1&&my_id==3){mutate_pages(p59,2,config,iter);set_step(100+iter*10+1);}
 if(config==0&&my_id==1){mutate_pages(p59,2,config,iter);set_step(100+iter*10+1);}
}

static void verify_config(const struct rk_status*s,unsigned config){
 if(s->owner1!=0||s->owner2!=2)_exit(90);
 if(config==0){if(s->owner5!=1||s->owner9!=1)_exit(91);}
 else{if(s->owner5!=3||s->owner9!=3)_exit(92);}
}

static uint64_t run_task(unsigned mode,unsigned iter,unsigned target){
 unsigned j;uint8_t*buf=aligned_alloc(PAGE_SZ,PAGE_SZ);struct rk_status a,b;uint64_t h=0xcbf29ce484222325ULL;if(!buf)die("task alloc");get_status(&a);
 for(j=0;j<4;j++){acquire_page(task_pages[j],MODE_READ,buf);h=fnv_update(h,buf,PAGE_SZ);}
 get_status(&b);free(buf);
 printf("RKMESH_V8_TASK_RESULT mode=%u iter=%u target=%u remote_xfers=%u remote_bytes=%llu local_hits=%u checksum=0x%016llx\n",mode,iter,target,b.page_xfers-a.page_xfers,(unsigned long long)(b.page_bytes-a.page_bytes),local_hits_sum(&b)-local_hits_sum(&a),(unsigned long long)h);
 if(h!=EXPECTED_TASK_CHECKSUM)_exit(93);
 return h;
}

static unsigned config_for(unsigned mode,unsigned iter){if(mode==NEG_HYST||mode==NEG_GREEDY)return iter&1u;return iter==6?1u:0u;}
static unsigned iterations_for(unsigned mode){return (mode==NEG_HYST||mode==NEG_GREEDY)?6u:7u;}
static const char*mode_name(unsigned mode){static const char*n[]={"neg_hyst","neg_greedy","pos_hyst","pos_stay"};return mode<4?n[mode]:"bad";}

int main(void){
 struct rk_status before_mut,after_mut,after_task;uint32_t costs[4];unsigned mode,iters,iter,config,best,current=0,current_before,target,migrations=0,migrated,advantage,streak_credit=0;int prev=-1,streak_best=-1;uint64_t mutation_bytes=0,task_remote_bytes=0,modeled_migration_bytes=0;uint32_t task_local_hits=0;
 setvbuf(stdout,NULL,_IONBF,0);setvbuf(stderr,NULL,_IONBF,0);setup_fs();loadmod();fd=opendev();if(fd<0)die("open");if(ioctl(fd,RK_WAIT_READY))die("ready");get_status(&after_task);my_id=after_task.id;mode=cmd_u("v8_mode",99);if(mode>3)_exit(84);iters=iterations_for(mode);
 printf("RKMESH_V8_USER_READY id=%u mode=%s iterations=%u\n",my_id,mode_name(mode),iters);
 for(iter=0;iter<iters;iter++){
   uint32_t start=100+iter*10;config=config_for(mode,iter);
   if(my_id==0){get_status(&before_mut);set_step(start);}else wait_step(start);
   perform_mutation(config,prev,iter);wait_step(start+1);
   if(my_id==0){
     uint64_t mut_delta;get_status(&after_mut);verify_config(&after_mut,config);mut_delta=after_mut.page_bytes-before_mut.page_bytes;mutation_bytes+=mut_delta;
     best=best_guest(&after_mut,costs);current_before=current;migrated=0;advantage=costs[current]>costs[best]?costs[current]-costs[best]:0;
     if(mode==NEG_GREEDY){if(best!=current){current=best;migrations++;modeled_migration_bytes+=MOVE_COST;migrated=1;}streak_credit=0;streak_best=-1;}
     else if(mode==POS_STAY){streak_credit=0;streak_best=-1;}
     else if(best==current||advantage==0){streak_credit=0;streak_best=-1;}
     else{
       if(streak_best==(int)best)streak_credit+=advantage;else{streak_best=(int)best;streak_credit=advantage;}
       if(streak_credit>MOVE_COST){current=best;migrations++;modeled_migration_bytes+=MOVE_COST;migrated=1;streak_credit=0;streak_best=-1;}
     }
     target=current;if(ioctl(fd,RK_SET_TARGET,&target))die("set target");
     printf("RKMESH_V8_DECISION mode=%s iter=%u config=%c current_before=%u best=%u current_after=%u c0=%u c1=%u c2=%u c3=%u advantage=%u credit=%u migrated=%u migrations=%u mutation_bytes=%llu\n",mode_name(mode),iter,config?'B':'A',current_before,best,current,costs[0],costs[1],costs[2],costs[3],advantage,streak_credit,migrated,migrations,(unsigned long long)mut_delta);
     set_step(start+2);
   } else wait_step(start+2);
   get_status(&after_mut);target=after_mut.task_target?after_mut.task_target-1:99;if(target>=4)_exit(94);
   if(my_id==target){run_task(mode,iter,target);set_step(start+3);}wait_step(start+3);
   if(my_id==0){
     uint64_t task_delta;uint32_t hit_delta,expect;get_status(&after_task);task_delta=after_task.page_bytes-after_mut.page_bytes;hit_delta=local_hits_sum(&after_task)-local_hits_sum(&after_mut);expect=remote_cost(&after_mut,current);
     if(task_delta!=expect||hit_delta!=4-expect/PAGE_SZ)_exit(95);
     task_remote_bytes+=task_delta;task_local_hits+=hit_delta;
     printf("RKMESH_V8_ACCOUNT mode=%s iter=%u host=%u task_remote_bytes=%llu task_local_hits=%u cumulative_task_remote=%llu modeled_migration=%llu\n",mode_name(mode),iter,current,(unsigned long long)task_delta,hit_delta,(unsigned long long)task_remote_bytes,(unsigned long long)modeled_migration_bytes);
   }
   prev=(int)config;
 }
 if(my_id==0){
   uint64_t modeled_total=task_remote_bytes+modeled_migration_bytes,exp_task=0,exp_state=0,exp_mut=0,exp_total=0;unsigned exp_mig=0,exp_host=0;
   if(mode==NEG_HYST){exp_task=73728;exp_state=0;exp_mut=45056;exp_total=73728;exp_mig=0;exp_host=0;}
   if(mode==NEG_GREEDY){exp_task=49152;exp_state=49152;exp_mut=45056;exp_total=98304;exp_mig=6;exp_host=3;}
   if(mode==POS_HYST){exp_task=73728;exp_state=8192;exp_mut=12288;exp_total=81920;exp_mig=1;exp_host=1;}
   if(mode==POS_STAY){exp_task=86016;exp_state=0;exp_mut=12288;exp_total=86016;exp_mig=0;exp_host=0;}
   if(task_remote_bytes!=exp_task||modeled_migration_bytes!=exp_state||mutation_bytes!=exp_mut||modeled_total!=exp_total||migrations!=exp_mig||current!=exp_host)_exit(96);
   printf("RKMESH_V8_SUMMARY mode=%s iterations=%u migrations=%u final_host=%u task_remote_bytes=%llu task_local_hits=%u modeled_migration_bytes=%llu modeled_total=%llu mutation_control_bytes=%llu\n",mode_name(mode),iters,migrations,current,(unsigned long long)task_remote_bytes,task_local_hits,(unsigned long long)modeled_migration_bytes,(unsigned long long)modeled_total,(unsigned long long)mutation_bytes);
 }
 printf("RKMESH_V8_PASS id=%u mode=%s\n",my_id,mode_name(mode));sync();reboot(RB_POWER_OFF);for(;;)pause();
}
