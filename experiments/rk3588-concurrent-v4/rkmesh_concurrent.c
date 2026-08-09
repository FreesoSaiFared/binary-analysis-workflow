#include <linux/module.h>
#include <linux/pci.h>
#include <linux/interrupt.h>
#include <linux/workqueue.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include <linux/io.h>
#include <linux/delay.h>
#include <linux/slab.h>
#include <linux/ktime.h>
#include <linux/mutex.h>
#include <linux/bitops.h>

#define RK_VENDOR 0x1af4
#define RK_DEVICE 0x1110
#define OFF_POS 8
#define OFF_DOORBELL 12
#define NODES 4
#define NPAGES 16
#define NSLOTS 4
#define LEASE_BYTES (4 * PAGE_SIZE)

#define READY_BASE       0x000
#define INIT_OFF         0x020
#define STEP_OFF         0x024
#define DONE_OFF         0x028
#define FINAL_OFF        0x030
#define OVERLAPS_OFF     0x038
#define DELAY_PROOF_OFF  0x03c
#define OWNER_BASE       0x100
#define READERS_BASE     0x140
#define BUSY_BASE        0x180
#define SUM_BASE         0x200
#define LEASE_OWNER_OFF  0x2a0
#define LEASE_SUM_OFF    0x2a8
#define LEASE_BUSY_OFF   0x2b0
#define PHASE_DONE_BASE  0x2c0
#define GATE_OFF         0x2d0
#define SLOT_BASE        0x400
#define SLOT_STRIDE      0x080
#define PAGE_TRANSFER_BASE  0x1000
#define LEASE_TRANSFER_BASE 0x10000

#define SO_STATE         0x00
#define SO_MODE          0x04
#define SO_PAGE          0x08
#define SO_REQUESTER     0x0c
#define SO_TARGET_OWNER  0x10
#define SO_TARGET_MASK   0x14
#define SO_GEN           0x18
#define SO_RESPONSE_OWNER 0x1c
#define SO_DELAY_MS      0x20
#define SO_RETRIES       0x24
#define SO_GATE          0x28
#define SO_PAGE_XFERS    0x2c
#define SO_LEASE_XFERS   0x30
#define SO_INVALIDATIONS 0x34
#define SO_PAGE_BYTES    0x38
#define SO_LEASE_BYTES   0x40
#define SO_ACK_BASE      0x48

#define S_IDLE 0U
#define S_REQUEST 1U
#define S_PROCESSING 2U
#define S_INVALIDATE 3U
#define S_RESPONSE 4U
#define S_RETRY 5U
#define S_DONE 6U

#define MODE_READ 1U
#define MODE_WRITE 2U
#define MODE_LEASE 3U
#define READY_MAGIC 0x35880000U
#define INIT_MAGIC 0x434f4e34U
#define DONE_MAGIC 0x600df004U

#define RK_IOC_MAGIC 'N'
struct rk_xfer {
    __u64 user_ptr;
    __u64 latency_ns;
    __u32 index, mode, from, irq_count;
    __u32 gate, delay_ms, retries, generation;
};
struct rk_inv { __u32 slot, page, generation, reserved; };
struct rk_gate { __u32 gate, slot_mask; };
struct rk_phase { __u32 phase, node_mask; };
struct rk_status {
    __u32 id, step, done, irq_count;
    __u32 owner0, owner2, owner4;
    __u32 readers0, readers2;
    __u32 busy0, busy2, busy4;
    __u32 overlaps, delay_proof, lease_owner, lease_busy;
    __u32 page_xfers, lease_xfers, invalidations, slot2_retries;
    __u64 page_bytes, lease_bytes, final_checksum, lease_checksum;
};
#define RK_WAIT_READY      _IO(RK_IOC_MAGIC,1)
#define RK_GET_STATUS      _IOR(RK_IOC_MAGIC,2,struct rk_status)
#define RK_ACQUIRE         _IOWR(RK_IOC_MAGIC,3,struct rk_xfer)
#define RK_COMMIT          _IOW(RK_IOC_MAGIC,4,struct rk_xfer)
#define RK_WAIT_INVALIDATE _IOR(RK_IOC_MAGIC,5,struct rk_inv)
#define RK_ACK_INVALIDATE  _IOW(RK_IOC_MAGIC,6,struct rk_inv)
#define RK_WAIT_STEP       _IOW(RK_IOC_MAGIC,7,__u32)
#define RK_SET_STEP        _IOW(RK_IOC_MAGIC,8,__u32)
#define RK_GATE_RELEASE    _IOW(RK_IOC_MAGIC,9,struct rk_gate)
#define RK_ASSERT_DELAY    _IO(RK_IOC_MAGIC,10)
#define RK_MARK_PHASE      _IOW(RK_IOC_MAGIC,11,__u32)
#define RK_WAIT_PHASE      _IOW(RK_IOC_MAGIC,12,struct rk_phase)
#define RK_FINALIZE        _IO(RK_IOC_MAGIC,13)
#define RK_WAIT_DONE       _IO(RK_IOC_MAGIC,14)

struct rkmesh_dev;
struct rk_slot_work { struct work_struct work; struct rkmesh_dev *d; u32 slot; };
struct rkmesh_dev {
    struct pci_dev *pdev;
    void __iomem *bar0, *bar2;
    u32 id;
    int irq;
    atomic_t irq_count;
    wait_queue_head_t resp_wq, inv_wq, ack_wq;
    unsigned long pending_inv_mask;
    struct mutex req_lock;
    struct mutex page_lock[NPAGES];
    struct mutex lease_lock;
    struct rk_slot_work sw[NSLOTS];
    u8 *pages, *lease;
    struct miscdevice misc;
};
static struct rkmesh_dev *gdev;

static inline void __iomem *slotp(struct rkmesh_dev *d,u32 s,u32 off){return d->bar2+SLOT_BASE+s*SLOT_STRIDE+off;}
static inline void ring_peer(struct rkmesh_dev *d,u32 peer){writel(peer<<16,d->bar0+OFF_DOORBELL);}
static inline u32 page_owner(struct rkmesh_dev*d,u32 p){return readl(d->bar2+OWNER_BASE+p*4);}
static inline u32 page_readers(struct rkmesh_dev*d,u32 p){return readl(d->bar2+READERS_BASE+p*4);}
static inline u32 page_busy(struct rkmesh_dev*d,u32 p){return readl(d->bar2+BUSY_BASE+p*4);}
static inline u8 *local_page(struct rkmesh_dev*d,u32 p){return d->pages+(size_t)p*PAGE_SIZE;}
static inline void add_slot_u32(struct rkmesh_dev*d,u32 s,u32 off,u32 v){writel(readl(slotp(d,s,off))+v,slotp(d,s,off));}
static inline void add_slot_u64(struct rkmesh_dev*d,u32 s,u32 off,u64 v){writeq(readq(slotp(d,s,off))+v,slotp(d,s,off));}
static inline void __iomem *page_transfer(struct rkmesh_dev*d,u32 s){return d->bar2+PAGE_TRANSFER_BASE+s*PAGE_SIZE;}
static inline void __iomem *lease_transfer(struct rkmesh_dev*d,u32 s){return d->bar2+LEASE_TRANSFER_BASE+s*LEASE_BYTES;}

static u64 checksum(const u8*p,size_t n){u64 h=0xcbf29ce484222325ULL;size_t i;for(i=0;i<n;i++){h^=p[i];h*=0x100000001b3ULL;}return h;}
static void init_page_bytes(u8*p,u32 pg){size_t i;for(i=0;i<PAGE_SIZE;i++)p[i]=(u8)((i*37U+11U+pg*13U)&0xffU);((u64*)p)[0]=0x3588358835883588ULL^((u64)pg*0x0101010101010101ULL);}
static void init_lease_bytes(u8*p){size_t i;for(i=0;i<LEASE_BYTES;i++)p[i]=(u8)((i*19U+7U)&0xffU);((u64*)p)[0]=0x1ea51ea51ea51ea5ULL;}
static bool all_ready(struct rkmesh_dev*d){u32 i;for(i=0;i<NODES;i++)if(readl(d->bar2+READY_BASE+i*4)!=(READY_MAGIC|i))return false;return true;}
static int wait_ready(struct rkmesh_dev*d){int i;for(i=0;i<5000;i++){if(all_ready(d))return 0;msleep(1);}return -ETIMEDOUT;}
static int wait_gate(struct rkmesh_dev*d,u32 gate){int i;if(!gate)return 0;for(i=0;i<5000;i++){if(readl(d->bar2+GATE_OFF)>=gate)return 0;msleep(1);}return -ETIMEDOUT;}
static bool acked(struct rkmesh_dev*d,u32 s,u32 targets,u32 gen){u32 i;for(i=0;i<NODES;i++)if((targets&(1U<<i))&&readl(slotp(d,s,SO_ACK_BASE+i*4))!=gen)return false;return true;}
static bool lower_priority_active(struct rkmesh_dev*d,u32 s,u32 page){u32 j,st;for(j=0;j<s;j++){st=readl(slotp(d,j,SO_STATE));if((st==S_REQUEST||st==S_PROCESSING||st==S_INVALIDATE)&&readl(slotp(d,j,SO_TARGET_OWNER))==d->id&&readl(slotp(d,j,SO_MODE))!=MODE_LEASE&&readl(slotp(d,j,SO_PAGE))==page)return true;}return false;}
static void send_retry(struct rkmesh_dev*d,u32 s,u32 req,u32 new_owner,const char*why){writel(new_owner,slotp(d,s,SO_RESPONSE_OWNER));wmb();writel(S_RETRY,slotp(d,s,SO_STATE));wmb();ring_peer(d,req);pr_info("RKMESH_V4_RETRY_ISSUED service=%u slot=%u requester=%u new_owner=%u why=%s gen=%u\n",d->id,s,req,new_owner,why,readl(slotp(d,s,SO_GEN)));}

static void service_slot(struct work_struct *work)
{
    struct rk_slot_work *sw=container_of(work,struct rk_slot_work,work);
    struct rkmesh_dev*d=sw->d;u32 s=sw->slot,st,mode,p,req,target,gen,gate,delay,owner,readers,targets,i,busy;
    st=readl(slotp(d,s,SO_STATE));if(st!=S_REQUEST||readl(slotp(d,s,SO_TARGET_OWNER))!=d->id)return;
    mode=readl(slotp(d,s,SO_MODE));p=readl(slotp(d,s,SO_PAGE));req=readl(slotp(d,s,SO_REQUESTER));target=readl(slotp(d,s,SO_TARGET_OWNER));gen=readl(slotp(d,s,SO_GEN));gate=readl(slotp(d,s,SO_GATE));delay=readl(slotp(d,s,SO_DELAY_MS));
    if(req>=NODES||req==d->id||target!=d->id){pr_err("RKMESH_V4_SERVICE_BAD service=%u slot=%u req=%u target=%u\n",d->id,s,req,target);return;}
    writel(S_PROCESSING,slotp(d,s,SO_STATE));wmb();
    if(wait_gate(d,gate)){pr_err("RKMESH_V4_GATE_TIMEOUT service=%u slot=%u gate=%u\n",d->id,s,gate);return;}
    if(delay)msleep(delay);
    if(mode==MODE_LEASE){
        mutex_lock(&d->lease_lock);owner=readl(d->bar2+LEASE_OWNER_OFF);busy=readl(d->bar2+LEASE_BUSY_OFF);
        if(owner!=d->id){mutex_unlock(&d->lease_lock);send_retry(d,s,req,owner,"owner-moved");return;}
        if(busy){mutex_unlock(&d->lease_lock);send_retry(d,s,req,owner,"lease-busy");return;}
        memcpy_toio(lease_transfer(d,s),d->lease,LEASE_BYTES);wmb();writel(req,d->bar2+LEASE_OWNER_OFF);writel(req+1,d->bar2+LEASE_BUSY_OFF);add_slot_u32(d,s,SO_LEASE_XFERS,1);add_slot_u64(d,s,SO_LEASE_BYTES,LEASE_BYTES);writel(d->id,slotp(d,s,SO_RESPONSE_OWNER));wmb();writel(S_RESPONSE,slotp(d,s,SO_STATE));wmb();mutex_unlock(&d->lease_lock);ring_peer(d,req);pr_info("RKMESH_V4_LEASE_TRANSFER slot=%u from=%u to=%u bytes=%lu gen=%u checksum=0x%016llx\n",s,d->id,req,(unsigned long)LEASE_BYTES,gen,(unsigned long long)checksum(d->lease,LEASE_BYTES));return;
    }
    if(p>=NPAGES){pr_err("RKMESH_V4_PAGE_BAD service=%u slot=%u page=%u\n",d->id,s,p);return;}
    for(i=0;i<5000&&lower_priority_active(d,s,p);i++)msleep(1);
    if(i==5000){pr_err("RKMESH_V4_PRIORITY_TIMEOUT service=%u slot=%u page=%u\n",d->id,s,p);return;}
    mutex_lock(&d->page_lock[p]);owner=page_owner(d,p);busy=page_busy(d,p);
    if(owner!=d->id){mutex_unlock(&d->page_lock[p]);send_retry(d,s,req,owner,"owner-moved");return;}
    if(busy){mutex_unlock(&d->page_lock[p]);send_retry(d,s,req,owner,"page-busy");return;}
    readers=page_readers(d,p);
    if(mode==MODE_WRITE){
        targets=readers&~(1U<<req);
        if(targets){
            writel(targets,slotp(d,s,SO_TARGET_MASK));for(i=0;i<NODES;i++)writel(0,slotp(d,s,SO_ACK_BASE+i*4));wmb();writel(S_INVALIDATE,slotp(d,s,SO_STATE));wmb();
            for(i=0;i<NODES;i++)if(targets&(1U<<i)){ring_peer(d,i);add_slot_u32(d,s,SO_INVALIDATIONS,1);pr_info("RKMESH_V4_INVALIDATE_SEND service=%u slot=%u page=%u target=%u gen=%u\n",d->id,s,p,i,gen);}
            if(!wait_event_timeout(d->ack_wq,acked(d,s,targets,gen),msecs_to_jiffies(3000))){pr_err("RKMESH_V4_INVALIDATE_TIMEOUT service=%u slot=%u page=%u gen=%u\n",d->id,s,p,gen);mutex_unlock(&d->page_lock[p]);return;}
            pr_info("RKMESH_V4_INVALIDATE_ALL_ACK service=%u slot=%u page=%u mask=0x%x gen=%u\n",d->id,s,p,targets,gen);writel(S_PROCESSING,slotp(d,s,SO_STATE));
        }
    }
    memcpy_toio(page_transfer(d,s),local_page(d,p),PAGE_SIZE);wmb();add_slot_u32(d,s,SO_PAGE_XFERS,1);add_slot_u64(d,s,SO_PAGE_BYTES,PAGE_SIZE);
    if(mode==MODE_READ)writel(readers|(1U<<req),d->bar2+READERS_BASE+p*4);else{writel(req,d->bar2+OWNER_BASE+p*4);writel(0,d->bar2+READERS_BASE+p*4);writel(req+1,d->bar2+BUSY_BASE+p*4);}
    writel(d->id,slotp(d,s,SO_RESPONSE_OWNER));wmb();writel(S_RESPONSE,slotp(d,s,SO_STATE));wmb();mutex_unlock(&d->page_lock[p]);ring_peer(d,req);
    pr_info("RKMESH_V4_PAGE_TRANSFER mode=%s slot=%u from=%u to=%u page=%u bytes=%lu gen=%u checksum=0x%016llx readers_before=0x%x\n",mode==MODE_READ?"READ":"WRITE",s,d->id,req,p,PAGE_SIZE,gen,(unsigned long long)checksum(local_page(d,p),PAGE_SIZE),readers);
}

static irqreturn_t rkmesh_irq(int irq,void*opaque)
{
    struct rkmesh_dev*d=opaque;u32 s,st,req,target,targets,gen;int c=atomic_inc_return(&d->irq_count);bool handled=false;
    for(s=0;s<NSLOTS;s++){
        st=readl(slotp(d,s,SO_STATE));req=readl(slotp(d,s,SO_REQUESTER));target=readl(slotp(d,s,SO_TARGET_OWNER));gen=readl(slotp(d,s,SO_GEN));
        if(st==S_REQUEST&&target==d->id){schedule_work(&d->sw[s].work);handled=true;}
        if((st==S_RESPONSE||st==S_RETRY)&&req==d->id){wake_up(&d->resp_wq);handled=true;}
        if(st==S_INVALIDATE){targets=readl(slotp(d,s,SO_TARGET_MASK));if((targets&(1U<<d->id))&&readl(slotp(d,s,SO_ACK_BASE+d->id*4))!=gen){set_bit(s,&d->pending_inv_mask);wake_up(&d->inv_wq);handled=true;}if(target==d->id){wake_up(&d->ack_wq);handled=true;}}
    }
    pr_info("RKMESH_V4_IRQ id=%u count=%d irq=%d handled=%u\n",d->id,c,irq,handled?1:0);return IRQ_HANDLED;
}

static int post_and_wait(struct rkmesh_dev*d,struct rk_xfer*x)
{
    u32 s=d->id,owner,gen,st,retries=0,gate=x->gate,delay=x->delay_ms;int attempt;u64 start=ktime_get_ns();
    for(attempt=0;attempt<64;attempt++){
        owner=x->mode==MODE_LEASE?readl(d->bar2+LEASE_OWNER_OFF):page_owner(d,x->index);if(owner==d->id)return -EALREADY;
        gen=readl(slotp(d,s,SO_GEN))+1;writel(x->mode,slotp(d,s,SO_MODE));writel(x->index,slotp(d,s,SO_PAGE));writel(d->id,slotp(d,s,SO_REQUESTER));writel(owner,slotp(d,s,SO_TARGET_OWNER));writel(0,slotp(d,s,SO_TARGET_MASK));writel(gen,slotp(d,s,SO_GEN));writel(attempt?0:delay,slotp(d,s,SO_DELAY_MS));writel(retries,slotp(d,s,SO_RETRIES));writel(attempt?0:gate,slotp(d,s,SO_GATE));wmb();writel(S_REQUEST,slotp(d,s,SO_STATE));wmb();ring_peer(d,owner);
        pr_info("RKMESH_V4_REQUEST requester=%u slot=%u mode=%u page=%u owner=%u gen=%u gate=%u delay=%u\n",d->id,s,x->mode,x->index,owner,gen,attempt?0:gate,attempt?0:delay);
        if(!wait_event_timeout(d->resp_wq,((st=readl(slotp(d,s,SO_STATE)))==S_RESPONSE||st==S_RETRY)&&readl(slotp(d,s,SO_GEN))==gen,msecs_to_jiffies(4000)))return -ETIMEDOUT;
        st=readl(slotp(d,s,SO_STATE));if(st==S_RETRY){u32 n=readl(slotp(d,s,SO_RESPONSE_OWNER));retries++;writel(retries,slotp(d,s,SO_RETRIES));pr_info("RKMESH_V4_RETRY requester=%u slot=%u page=%u old_owner=%u new_owner=%u retries=%u gen=%u\n",d->id,s,x->index,owner,n,retries,gen);msleep(1);continue;}
        if(x->mode==MODE_LEASE){memcpy_fromio(d->lease,lease_transfer(d,s),LEASE_BYTES);if(copy_to_user((void __user*)(unsigned long)x->user_ptr,d->lease,LEASE_BYTES))return -EFAULT;}
        else{memcpy_fromio(local_page(d,x->index),page_transfer(d,s),PAGE_SIZE);if(copy_to_user((void __user*)(unsigned long)x->user_ptr,local_page(d,x->index),PAGE_SIZE))return -EFAULT;}
        x->latency_ns=ktime_get_ns()-start;x->from=readl(slotp(d,s,SO_RESPONSE_OWNER));x->irq_count=atomic_read(&d->irq_count);x->retries=retries;x->generation=gen;writel(S_DONE,slotp(d,s,SO_STATE));wmb();return 0;
    }return -EAGAIN;
}

static void fill_status(struct rkmesh_dev*d,struct rk_status*st)
{
    u32 s;memset(st,0,sizeof(*st));st->id=d->id;st->step=readl(d->bar2+STEP_OFF);st->done=readl(d->bar2+DONE_OFF);st->irq_count=atomic_read(&d->irq_count);st->owner0=page_owner(d,0);st->owner2=page_owner(d,2);st->owner4=page_owner(d,4);st->readers0=page_readers(d,0);st->readers2=page_readers(d,2);st->busy0=page_busy(d,0);st->busy2=page_busy(d,2);st->busy4=page_busy(d,4);st->overlaps=readl(d->bar2+OVERLAPS_OFF);st->delay_proof=readl(d->bar2+DELAY_PROOF_OFF);st->lease_owner=readl(d->bar2+LEASE_OWNER_OFF);st->lease_busy=readl(d->bar2+LEASE_BUSY_OFF);st->final_checksum=readq(d->bar2+FINAL_OFF);st->lease_checksum=readq(d->bar2+LEASE_SUM_OFF);st->slot2_retries=readl(slotp(d,2,SO_RETRIES));for(s=0;s<NSLOTS;s++){st->page_xfers+=readl(slotp(d,s,SO_PAGE_XFERS));st->lease_xfers+=readl(slotp(d,s,SO_LEASE_XFERS));st->invalidations+=readl(slotp(d,s,SO_INVALIDATIONS));st->page_bytes+=readq(slotp(d,s,SO_PAGE_BYTES));st->lease_bytes+=readq(slotp(d,s,SO_LEASE_BYTES));}}

static long rk_ioctl(struct file*file,unsigned int cmd,unsigned long arg)
{
    struct rkmesh_dev*d=gdev;struct rk_xfer x;struct rk_status st;struct rk_inv inv;struct rk_gate gc;struct rk_phase ph;u32 v,p,mode,s,gen,targets,service;int i,rc;u64 h;if(!d)return -ENODEV;
    switch(cmd){
    case RK_WAIT_READY:return wait_ready(d);
    case RK_GET_STATUS:fill_status(d,&st);return copy_to_user((void __user*)arg,&st,sizeof(st))?-EFAULT:0;
    case RK_ACQUIRE:if(copy_from_user(&x,(void __user*)arg,sizeof(x)))return -EFAULT;if((x.mode!=MODE_READ&&x.mode!=MODE_WRITE&&x.mode!=MODE_LEASE)||(x.mode!=MODE_LEASE&&x.index>=NPAGES))return -EINVAL;rc=mutex_lock_interruptible(&d->req_lock);if(rc)return rc;rc=post_and_wait(d,&x);mutex_unlock(&d->req_lock);if(rc)return rc;return copy_to_user((void __user*)arg,&x,sizeof(x))?-EFAULT:0;
    case RK_COMMIT:
        if(copy_from_user(&x,(void __user*)arg,sizeof(x)))return -EFAULT;p=x.index;mode=x.mode;
        if(mode==MODE_LEASE){mutex_lock(&d->lease_lock);if(readl(d->bar2+LEASE_OWNER_OFF)!=d->id||readl(d->bar2+LEASE_BUSY_OFF)!=d->id+1){mutex_unlock(&d->lease_lock);return -EPERM;}if(copy_from_user(d->lease,(void __user*)(unsigned long)x.user_ptr,LEASE_BYTES)){mutex_unlock(&d->lease_lock);return -EFAULT;}h=checksum(d->lease,LEASE_BYTES);writeq(h,d->bar2+LEASE_SUM_OFF);writel(0,d->bar2+LEASE_BUSY_OFF);wmb();mutex_unlock(&d->lease_lock);pr_info("RKMESH_V4_LEASE_COMMIT id=%u checksum=0x%016llx\n",d->id,(unsigned long long)h);return 0;}
        if(mode!=MODE_WRITE||p>=NPAGES)return -EINVAL;mutex_lock(&d->page_lock[p]);if(page_owner(d,p)!=d->id||page_busy(d,p)!=d->id+1){mutex_unlock(&d->page_lock[p]);return -EPERM;}if(copy_from_user(local_page(d,p),(void __user*)(unsigned long)x.user_ptr,PAGE_SIZE)){mutex_unlock(&d->page_lock[p]);return -EFAULT;}h=checksum(local_page(d,p),PAGE_SIZE);writeq(h,d->bar2+SUM_BASE+p*8);writel(0,d->bar2+BUSY_BASE+p*4);wmb();mutex_unlock(&d->page_lock[p]);pr_info("RKMESH_V4_PAGE_COMMIT id=%u page=%u checksum=0x%016llx\n",d->id,p,(unsigned long long)h);return 0;
    case RK_WAIT_INVALIDATE:
        if(!wait_event_timeout(d->inv_wq,READ_ONCE(d->pending_inv_mask)!=0,msecs_to_jiffies(6000)))return -ETIMEDOUT;s=__ffs(d->pending_inv_mask);clear_bit(s,&d->pending_inv_mask);inv.slot=s;inv.page=readl(slotp(d,s,SO_PAGE));inv.generation=readl(slotp(d,s,SO_GEN));inv.reserved=0;return copy_to_user((void __user*)arg,&inv,sizeof(inv))?-EFAULT:0;
    case RK_ACK_INVALIDATE:
        if(copy_from_user(&inv,(void __user*)arg,sizeof(inv)))return -EFAULT;s=inv.slot;if(s>=NSLOTS)return -EINVAL;p=readl(slotp(d,s,SO_PAGE));gen=readl(slotp(d,s,SO_GEN));targets=readl(slotp(d,s,SO_TARGET_MASK));if(readl(slotp(d,s,SO_STATE))!=S_INVALIDATE||p!=inv.page||gen!=inv.generation||!(targets&(1U<<d->id)))return -EINVAL;writel(gen,slotp(d,s,SO_ACK_BASE+d->id*4));wmb();service=readl(slotp(d,s,SO_TARGET_OWNER));ring_peer(d,service);pr_info("RKMESH_V4_INVALIDATE_ACK id=%u slot=%u page=%u service=%u gen=%u\n",d->id,s,p,service,gen);return 0;
    case RK_WAIT_STEP:if(copy_from_user(&v,(void __user*)arg,sizeof(v)))return -EFAULT;for(i=0;i<10000;i++){if(readl(d->bar2+STEP_OFF)>=v)return 0;msleep(1);}return -ETIMEDOUT;
    case RK_SET_STEP:if(copy_from_user(&v,(void __user*)arg,sizeof(v)))return -EFAULT;writel(v,d->bar2+STEP_OFF);wmb();pr_info("RKMESH_V4_STEP id=%u step=%u\n",d->id,v);return 0;
    case RK_GATE_RELEASE:
        if(copy_from_user(&gc,(void __user*)arg,sizeof(gc)))return -EFAULT;for(i=0;i<5000;i++){bool ok=true;for(s=0;s<NSLOTS;s++)if(gc.slot_mask&(1U<<s)){v=readl(slotp(d,s,SO_STATE));if(v!=S_REQUEST&&v!=S_PROCESSING){ok=false;break;}}if(ok)break;msleep(1);}if(i==5000)return -ETIMEDOUT;writel(readl(d->bar2+OVERLAPS_OFF)+1,d->bar2+OVERLAPS_OFF);pr_info("RKMESH_V4_OVERLAP_PROVEN controller=%u gate=%u mask=0x%x states=%u,%u,%u,%u gens=%u,%u,%u,%u\n",d->id,gc.gate,gc.slot_mask,readl(slotp(d,0,SO_STATE)),readl(slotp(d,1,SO_STATE)),readl(slotp(d,2,SO_STATE)),readl(slotp(d,3,SO_STATE)),readl(slotp(d,0,SO_GEN)),readl(slotp(d,1,SO_GEN)),readl(slotp(d,2,SO_GEN)),readl(slotp(d,3,SO_GEN)));wmb();writel(gc.gate,d->bar2+GATE_OFF);wmb();return 0;
    case RK_ASSERT_DELAY:
        msleep(8);if(readl(slotp(d,1,SO_STATE))!=S_PROCESSING)return -EUCLEAN;v=readl(slotp(d,3,SO_STATE));if(v!=S_RESPONSE&&v!=S_DONE)return -EUCLEAN;writel(1,d->bar2+DELAY_PROOF_OFF);wmb();pr_info("RKMESH_V4_DELAY_ISOLATION_PASS controller=%u delayed_slot=1 fast_slot=3 fast_state=%u\n",d->id,v);return 0;
    case RK_MARK_PHASE:if(copy_from_user(&v,(void __user*)arg,sizeof(v)))return -EFAULT;writel(v,d->bar2+PHASE_DONE_BASE+d->id*4);wmb();pr_info("RKMESH_V4_PHASE_DONE id=%u phase=%u\n",d->id,v);return 0;
    case RK_WAIT_PHASE:
        if(copy_from_user(&ph,(void __user*)arg,sizeof(ph)))return -EFAULT;for(i=0;i<10000;i++){bool ok=true;for(s=0;s<NODES;s++)if((ph.node_mask&(1U<<s))&&readl(d->bar2+PHASE_DONE_BASE+s*4)<ph.phase){ok=false;break;}if(ok)return 0;msleep(1);}return -ETIMEDOUT;
    case RK_FINALIZE:
        if(d->id!=0)return -EPERM;h=0xcbf29ce484222325ULL;for(p=0;p<NPAGES;p++){u64 z=readq(d->bar2+SUM_BASE+p*8);int b;for(b=0;b<8;b++){h^=(u8)(z>>(b*8));h*=0x100000001b3ULL;}}{u64 z=readq(d->bar2+LEASE_SUM_OFF);int b;for(b=0;b<8;b++){h^=(u8)(z>>(b*8));h*=0x100000001b3ULL;}}writeq(h,d->bar2+FINAL_OFF);writel(DONE_MAGIC,d->bar2+DONE_OFF);wmb();fill_status(d,&st);pr_info("RKMESH_V4_FINAL checksum=0x%016llx page_xfers=%u page_bytes=%llu invalidations=%u overlaps=%u delay_proof=%u retries_slot2=%u lease_xfers=%u lease_bytes=%llu\n",(unsigned long long)h,st.page_xfers,(unsigned long long)st.page_bytes,st.invalidations,st.overlaps,st.delay_proof,st.slot2_retries,st.lease_xfers,(unsigned long long)st.lease_bytes);return 0;
    case RK_WAIT_DONE:for(i=0;i<10000;i++){if(readl(d->bar2+DONE_OFF)==DONE_MAGIC)return 0;msleep(1);}return -ETIMEDOUT;
    default:return -ENOTTY;}
}
static const struct file_operations fops={.owner=THIS_MODULE,.unlocked_ioctl=rk_ioctl,
#ifdef CONFIG_COMPAT
.compat_ioctl=rk_ioctl,
#endif
};

static int rk_probe(struct pci_dev*pdev,const struct pci_device_id*id)
{
    struct rkmesh_dev*d;int rc,i;u32 p,s;u64 h;d=devm_kzalloc(&pdev->dev,sizeof(*d),GFP_KERNEL);if(!d)return -ENOMEM;d->pdev=pdev;pci_set_drvdata(pdev,d);if((rc=pcim_enable_device(pdev)))return rc;pci_set_master(pdev);if((rc=pcim_iomap_regions(pdev,BIT(0)|BIT(2),"rkmesh_concurrent")))return rc;d->bar0=pcim_iomap_table(pdev)[0];d->bar2=pcim_iomap_table(pdev)[2];if(!d->bar0||!d->bar2)return -ENODEV;d->id=readl(d->bar0+OFF_POS);if(d->id>=NODES)return -EINVAL;
    d->pages=devm_kzalloc(&pdev->dev,(size_t)NPAGES*PAGE_SIZE,GFP_KERNEL);d->lease=devm_kzalloc(&pdev->dev,LEASE_BYTES,GFP_KERNEL);if(!d->pages||!d->lease)return -ENOMEM;init_waitqueue_head(&d->resp_wq);init_waitqueue_head(&d->inv_wq);init_waitqueue_head(&d->ack_wq);mutex_init(&d->req_lock);mutex_init(&d->lease_lock);for(p=0;p<NPAGES;p++)mutex_init(&d->page_lock[p]);atomic_set(&d->irq_count,0);d->pending_inv_mask=0;for(s=0;s<NSLOTS;s++){d->sw[s].d=d;d->sw[s].slot=s;INIT_WORK(&d->sw[s].work,service_slot);}
    if((rc=pci_alloc_irq_vectors(pdev,1,1,PCI_IRQ_MSIX))<0)return rc;d->irq=pci_irq_vector(pdev,0);if((rc=request_irq(d->irq,rkmesh_irq,0,"rkmesh_concurrent",d))){pci_free_irq_vectors(pdev);return rc;}
    d->misc.minor=MISC_DYNAMIC_MINOR;d->misc.name="rkmesh_concurrent";d->misc.fops=&fops;gdev=d;if((rc=misc_register(&d->misc))){gdev=NULL;free_irq(d->irq,d);pci_free_irq_vectors(pdev);return rc;}
    if(d->id==0){writel(0,d->bar2+STEP_OFF);writel(0,d->bar2+DONE_OFF);writeq(0,d->bar2+FINAL_OFF);writel(0,d->bar2+OVERLAPS_OFF);writel(0,d->bar2+DELAY_PROOF_OFF);writel(0,d->bar2+GATE_OFF);writel(0,d->bar2+LEASE_OWNER_OFF);writel(0,d->bar2+LEASE_BUSY_OFF);for(i=0;i<NODES;i++)writel(0,d->bar2+PHASE_DONE_BASE+i*4);for(s=0;s<NSLOTS;s++){memset_io(slotp(d,s,0),0,SLOT_STRIDE);writel(s,slotp(d,s,SO_REQUESTER));}for(p=0;p<NPAGES;p++){writel(p%NODES,d->bar2+OWNER_BASE+p*4);writel(0,d->bar2+READERS_BASE+p*4);writel(0,d->bar2+BUSY_BASE+p*4);init_page_bytes(local_page(d,p),p);h=checksum(local_page(d,p),PAGE_SIZE);writeq(h,d->bar2+SUM_BASE+p*8);}init_lease_bytes(d->lease);writeq(checksum(d->lease,LEASE_BYTES),d->bar2+LEASE_SUM_OFF);wmb();writel(INIT_MAGIC,d->bar2+INIT_OFF);wmb();}
    else{for(i=0;i<3000&&readl(d->bar2+INIT_OFF)!=INIT_MAGIC;i++)msleep(1);if(i==3000)return -ETIMEDOUT;for(p=0;p<NPAGES;p++)if(p%NODES==d->id)init_page_bytes(local_page(d,p),p);}
    if(d->id==0)init_lease_bytes(d->lease);writel(READY_MAGIC|d->id,d->bar2+READY_BASE+d->id*4);wmb();pr_info("RKMESH_V4_READY id=%u irq=%d\n",d->id,d->irq);return 0;
}
static void rk_remove(struct pci_dev*pdev){struct rkmesh_dev*d=pci_get_drvdata(pdev);u32 s;if(gdev==d)gdev=NULL;misc_deregister(&d->misc);for(s=0;s<NSLOTS;s++)cancel_work_sync(&d->sw[s].work);free_irq(d->irq,d);pci_free_irq_vectors(pdev);}
static const struct pci_device_id ids[]={{PCI_DEVICE(RK_VENDOR,RK_DEVICE)},{0,}};MODULE_DEVICE_TABLE(pci,ids);
static struct pci_driver rkmesh_concurrent_driver={.name="rkmesh_concurrent",.id_table=ids,.probe=rk_probe,.remove=rk_remove};module_pci_driver(rkmesh_concurrent_driver);MODULE_LICENSE("GPL");MODULE_DESCRIPTION("RKMesh concurrent transaction slots v4");
