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
#define TASK_STATE_BYTES 8192u
#define RK_IOC_MAGIC 'P'
struct rk_state_op{uint64_t user_ptr,checksum,latency_ns;uint32_t generation,from,irq_count,reserved;};
struct rk_exec_op{uint64_t user_ptr,checksum,wait_ns;uint32_t generation,destination,from,reserved;};
struct rk_status{uint32_t id,step,done,irq_count;uint32_t owner0,owner1,owner2,owner4,owner5,owner6,owner9;uint32_t busy0,busy2,busy4,busy6;uint32_t page_xfers;uint32_t local_hits0,local_hits1,local_hits2,local_hits3;uint32_t task_target,task_scenario;uint32_t state_owner,state_gen,state_xfers;uint32_t state_local_hits0,state_local_hits1,state_local_hits2,state_local_hits3;uint64_t page_bytes,state_bytes,state_checksum,final_checksum;uint32_t exec_state,exec_dest,exec_gen,exec_event_gen;uint32_t exec_waits,exec_wakes,exec_stale_ignored,old_owner_rejects;};
#define RK_WAIT_READY _IO(RK_IOC_MAGIC,1)
#define RK_GET_STATUS _IOR(RK_IOC_MAGIC,2,struct rk_status)
#define RK_STATE_ACQUIRE _IOWR(RK_IOC_MAGIC,23,struct rk_state_op)
#define RK_STATE_COMMIT _IOW(RK_IOC_MAGIC,24,struct rk_state_op)
#define RK_BARRIER _IOW(RK_IOC_MAGIC,25,uint32_t)
#define RK_EXEC_WAIT _IOWR(RK_IOC_MAGIC,26,struct rk_exec_op)
#define RK_EXEC_STALE _IOW(RK_IOC_MAGIC,27,struct rk_exec_op)
#define RK_EXEC_HANDOFF _IOW(RK_IOC_MAGIC,28,struct rk_exec_op)
static const uint64_t EXPECT0=0x5b48fce9c4e70c21ULL,EXPECT1=0x0602e7fe82ac09b0ULL,EXEC0=0x52873de4b5d717a5ULL,PAGE_TASK_CHECKSUM=0x4a9f55cc4def6f8dULL;
static int fd=-1;static unsigned my_id;
static void die(const char*m){perror(m);_exit(80);}static void setup_fs(void){mkdir("/proc",0555);mkdir("/sys",0555);mkdir("/dev",0755);if(mount("proc","/proc","proc",0,0)&&errno!=EBUSY)die("proc");if(mount("sysfs","/sys","sysfs",0,0)&&errno!=EBUSY)die("sys");if(mount("devtmpfs","/dev","devtmpfs",0,0)&&errno!=EBUSY)die("dev");}
static void loadmod(void){int f=open("/rkmesh_v10.ko",O_RDONLY),rc,e;if(f<0)die("module");rc=syscall(SYS_finit_module,f,"",0);e=errno;close(f);printf("RKMESH_V10_MODULE_LOAD rc=%d errno=%d\n",rc,e);if(rc)_exit(81);}static int opendev(void){int i,x;for(i=0;i<1000;i++){x=open("/dev/rkmesh_v10",O_RDWR);if(x>=0)return x;usleep(1000);}return -1;}
static void barrier(uint32_t p){if(ioctl(fd,RK_BARRIER,&p))die("barrier");}static void status(struct rk_status*s){if(ioctl(fd,RK_GET_STATUS,s))die("status");}
static unsigned mode(void){char b[1024],*p;int f=open("/proc/cmdline",O_RDONLY);ssize_t n;if(f<0)return 99;n=read(f,b,sizeof(b)-1);close(f);if(n<=0)return 99;b[n]=0;p=strstr(b,"v10_mode=");return p?(unsigned)strtoul(p+9,0,10):99;}
static uint64_t fnv(const uint8_t*p,size_t n){uint64_t h=0xcbf29ce484222325ULL;size_t i;for(i=0;i<n;i++){h^=p[i];h*=0x100000001b3ULL;}return h;}static uint64_t qget(const uint8_t*p,unsigned q){uint64_t v;memcpy(&v,p+q*8,8);return v;}static void qset(uint8_t*p,unsigned q,uint64_t v){memcpy(p+q*8,&v,8);}static uint64_t exec_value(const uint8_t*s){return PAGE_TASK_CHECKSUM^qget(s,4)^qget(s,5);}static void update_state(uint8_t*s){uint64_t v;v=qget(s,4)^(PAGE_TASK_CHECKSUM+0x9e3779b97f4a7c15ULL);qset(s,4,v);v=qget(s,5)+(PAGE_TASK_CHECKSUM^(1ULL*0xd6e8feb86659fd93ULL));qset(s,5,v);s[64]^=(uint8_t)PAGE_TASK_CHECKSUM;{uint32_t g=1;memcpy(s+8,&g,4);}}
static void state_acquire(uint8_t*s,struct rk_state_op*o){memset(o,0,sizeof(*o));o->user_ptr=(uintptr_t)s;if(ioctl(fd,RK_STATE_ACQUIRE,o))die("state acquire");if(o->generation!=0||o->checksum!=EXPECT0||fnv(s,TASK_STATE_BYTES)!=EXPECT0)_exit(90);}static void state_commit(uint8_t*s,struct rk_state_op*o){o->user_ptr=(uintptr_t)s;o->generation=1;o->checksum=fnv(s,TASK_STATE_BYTES);if(o->checksum!=EXPECT1)_exit(91);if(ioctl(fd,RK_STATE_COMMIT,o))die("state commit");}
static void execute_once(uint8_t*s,struct rk_state_op*o,const char*who){uint64_t e=exec_value(s);if(e!=EXEC0)_exit(92);update_state(s);state_commit(s,o);printf("RKMESH_V10_EXECUTED id=%u who=%s exec=0x%016llx committed_gen=1 checksum=0x%016llx\n",my_id,who,(unsigned long long)e,(unsigned long long)o->checksum);}
int main(void){struct rk_status st;unsigned m;uint8_t*state;struct rk_state_op sop;setvbuf(stdout,0,_IONBF,0);setvbuf(stderr,0,_IONBF,0);setup_fs();loadmod();fd=opendev();if(fd<0)die("open");if(ioctl(fd,RK_WAIT_READY))die("ready");status(&st);my_id=st.id;m=mode();if(m>1)_exit(82);state=aligned_alloc(4096,TASK_STATE_BYTES);if(!state)die("alloc");printf("RKMESH_V10_USER_READY id=%u mode=%s state_owner=%u gen=%u checksum=0x%016llx\n",my_id,m==0?"migrate_sleep":"stay_local",st.state_owner,st.state_gen,(unsigned long long)st.state_checksum);barrier(10);
if(m==0){
 if(my_id==1){struct rk_exec_op ex={.user_ptr=(uintptr_t)state,.generation=0,.destination=1};printf("RKMESH_V10_SLEEP_ENTER id=1 gen=0\n");if(ioctl(fd,RK_EXEC_WAIT,&ex))die("exec wait");if(ex.checksum!=EXPECT0||fnv(state,TASK_STATE_BYTES)!=EXPECT0)_exit(93);printf("RKMESH_V10_SLEEP_RETURN id=1 gen=0 checksum=0x%016llx wait_ns=%llu\n",(unsigned long long)ex.checksum,(unsigned long long)ex.wait_ns);memset(&sop,0,sizeof(sop));sop.user_ptr=(uintptr_t)state;sop.generation=0;sop.checksum=EXPECT0;execute_once(state,&sop,"destination");}
 if(my_id==0){struct rk_exec_op ex={.generation=0,.destination=1};int i;for(i=0;i<5000;i++){status(&st);if(st.exec_state==1&&st.exec_dest==1&&st.exec_waits==1)break;usleep(1000);if(i==4999)_exit(94);}if(ioctl(fd,RK_EXEC_STALE,&ex))die("stale");for(i=0;i<5000;i++){status(&st);if(st.exec_stale_ignored==1)break;usleep(1000);if(i==4999)_exit(95);}if(ioctl(fd,RK_EXEC_HANDOFF,&ex))die("handoff");for(i=0;i<5000;i++){status(&st);if(st.exec_state==3&&st.state_owner==1&&st.exec_wakes==1)break;usleep(1000);if(i==4999)_exit(96);}uint8_t*old=aligned_alloc(4096,TASK_STATE_BYTES);if(!old)die("old alloc");memset(&sop,0,sizeof(sop));sop.user_ptr=(uintptr_t)old;sop.generation=1;sop.checksum=0;errno=0;if(ioctl(fd,RK_STATE_COMMIT,&sop)==0||errno!=EPERM)_exit(97);printf("RKMESH_V10_OLD_HOST_NEGATIVE_PASS id=0 errno=%d\n",errno);free(old);}
 barrier(20);
 status(&st);if(st.state_owner!=1||st.state_gen!=1||st.state_checksum!=EXPECT1||st.state_xfers!=1||st.state_bytes!=8192||st.exec_waits!=1||st.exec_wakes!=1||st.exec_stale_ignored!=1||st.old_owner_rejects!=1)_exit(100);
}else{
 if(my_id==0){state_acquire(state,&sop);execute_once(state,&sop,"local");}
 barrier(20);status(&st);if(st.state_owner!=0||st.state_gen!=1||st.state_checksum!=EXPECT1||st.state_xfers!=0||st.state_bytes!=0||st.exec_waits!=0||st.exec_wakes!=0||st.exec_stale_ignored!=0||st.old_owner_rejects!=0)_exit(101);
}
printf("RKMESH_V10_PASS id=%u mode=%s owner=%u gen=%u state_xfers=%u state_bytes=%llu waits=%u wakes=%u stale=%u old_rejects=%u checksum=0x%016llx\n",my_id,m==0?"migrate_sleep":"stay_local",st.state_owner,st.state_gen,st.state_xfers,(unsigned long long)st.state_bytes,st.exec_waits,st.exec_wakes,st.exec_stale_ignored,st.old_owner_rejects,(unsigned long long)st.state_checksum);sync();reboot(RB_POWER_OFF);for(;;)pause();}
