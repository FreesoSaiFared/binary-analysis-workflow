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

#define READY_BASE       0x000
#define INIT_OFF         0x020
#define STEP_OFF         0x024
#define DONE_OFF         0x028
#define FINAL_OFF        0x030
#define OWNER_BASE       0x100
#define BUSY_BASE        0x140
#define LOCAL_HITS_BASE  0x220
#define TASK_TARGET_OFF  0x230
#define TASK_SCENARIO_OFF 0x234
#define SLOT_BASE        0x300
#define SLOT_STRIDE      0x040
#define TRANSFER_BASE    0x1000

#define SO_STATE          0x00
#define SO_MODE           0x04
#define SO_PAGE           0x08
#define SO_REQUESTER      0x0c
#define SO_TARGET_OWNER   0x10
#define SO_GEN            0x14
#define SO_RESPONSE_OWNER 0x18
#define SO_RETRIES        0x1c
#define SO_PAGE_XFERS     0x20
#define SO_PAGE_BYTES     0x28

#define S_IDLE 0U
#define S_REQUEST 1U
#define S_PROCESSING 2U
#define S_RESPONSE 3U
#define S_RETRY 4U
#define S_DONE 5U

#define MODE_READ 1U
#define MODE_WRITE 2U
#define READY_MAGIC 0x35880000U
#define INIT_MAGIC 0x53594e37U
#define DONE_MAGIC 0x600df007U

#define RK_IOC_MAGIC 'P'
struct rk_xfer {
    __u64 user_ptr;
    __u64 latency_ns;
    __u32 index, mode, from, irq_count;
    __u32 gate, delay_ms, retries, generation;
};
struct rk_status {
    __u32 id, step, done, irq_count;
    __u32 owner0, owner1, owner2, owner4, owner5, owner6, owner9;
    __u32 readers0, readers2;
    __u32 busy0, busy2, busy4, busy6;
    __u32 overlaps, delay_proof, lease_owner, lease_busy;
    __u32 page_xfers, lease_xfers, invalidations, slot2_retries;
    __u32 local_hits0, local_hits1, local_hits2, local_hits3;
    __u32 task_target, task_scenario;
    __u32 lock_owner, lock_wait_mask, cs_owner;
    __u32 lock_acquires, lock_waits, lock_wakes, lock_releases;
    __u32 stale_wakes, protected_rejects, cs_violations;
    __u64 page_bytes, lease_bytes, final_checksum, lease_checksum, protected_checksum;
};
#define RK_WAIT_READY   _IO(RK_IOC_MAGIC,1)
#define RK_GET_STATUS   _IOR(RK_IOC_MAGIC,2,struct rk_status)
#define RK_ACQUIRE      _IOWR(RK_IOC_MAGIC,3,struct rk_xfer)
#define RK_COMMIT       _IOW(RK_IOC_MAGIC,4,struct rk_xfer)
#define RK_WAIT_STEP    _IOW(RK_IOC_MAGIC,7,__u32)
#define RK_SET_STEP     _IOW(RK_IOC_MAGIC,8,__u32)
#define RK_WAIT_DONE    _IO(RK_IOC_MAGIC,14)
#define RK_SET_TARGET   _IOW(RK_IOC_MAGIC,20,__u32)
#define RK_SET_SCENARIO _IOW(RK_IOC_MAGIC,21,__u32)
#define RK_TASK_FINISH  _IOW(RK_IOC_MAGIC,22,__u64)

struct rkmesh_dev;
struct rk_slot_work { struct work_struct work; struct rkmesh_dev *d; u32 slot; };
struct rkmesh_dev {
    struct pci_dev *pdev;
    void __iomem *bar0, *bar2;
    u32 id;
    int irq;
    atomic_t irq_count;
    wait_queue_head_t resp_wq;
    struct mutex req_lock;
    struct mutex page_lock[NPAGES];
    struct rk_slot_work sw[NSLOTS];
    u8 *pages;
    struct miscdevice misc;
};
static struct rkmesh_dev *gdev;

static inline void __iomem *slotp(struct rkmesh_dev*d,u32 s,u32 off){return d->bar2+SLOT_BASE+s*SLOT_STRIDE+off;}
static inline void ring_peer(struct rkmesh_dev*d,u32 peer){writel(peer<<16,d->bar0+OFF_DOORBELL);}
static inline u32 page_owner(struct rkmesh_dev*d,u32 p){return readl(d->bar2+OWNER_BASE+p*4);}
static inline u32 page_busy(struct rkmesh_dev*d,u32 p){return readl(d->bar2+BUSY_BASE+p*4);}
static inline u8 *local_page(struct rkmesh_dev*d,u32 p){return d->pages+(size_t)p*PAGE_SIZE;}
static inline void __iomem *transferp(struct rkmesh_dev*d,u32 s){return d->bar2+TRANSFER_BASE+s*PAGE_SIZE;}
static inline void add_u32(void __iomem*p,u32 v){writel(readl(p)+v,p);}
static inline void add_u64(void __iomem*p,u64 v){writeq(readq(p)+v,p);}

static u64 checksum(const u8*p,size_t n){u64 h=0xcbf29ce484222325ULL;size_t i;for(i=0;i<n;i++){h^=p[i];h*=0x100000001b3ULL;}return h;}
static void init_page_bytes(u8*p,u32 pg){size_t i;for(i=0;i<PAGE_SIZE;i++)p[i]=(u8)((i*37U+11U+pg*13U)&0xffU);((u64*)p)[0]=0x3588358835883588ULL^((u64)pg*0x0101010101010101ULL);}
static bool all_ready(struct rkmesh_dev*d){u32 i;for(i=0;i<NODES;i++)if(readl(d->bar2+READY_BASE+i*4)!=(READY_MAGIC|i))return false;return true;}
static int wait_ready(struct rkmesh_dev*d){int i;for(i=0;i<5000;i++){if(all_ready(d))return 0;msleep(1);}return -ETIMEDOUT;}

static void send_retry(struct rkmesh_dev*d,u32 s,u32 req,u32 owner)
{
    writel(owner,slotp(d,s,SO_RESPONSE_OWNER));wmb();writel(S_RETRY,slotp(d,s,SO_STATE));wmb();ring_peer(d,req);
    pr_info("RKMESH_V7_RETRY service=%u slot=%u requester=%u new_owner=%u gen=%u\n",d->id,s,req,owner,readl(slotp(d,s,SO_GEN)));
}

static void service_slot(struct work_struct *work)
{
    struct rk_slot_work *sw=container_of(work,struct rk_slot_work,work);
    struct rkmesh_dev*d=sw->d;u32 s=sw->slot,st,mode,p,req,target,gen,owner,busy;
    st=readl(slotp(d,s,SO_STATE));if(st!=S_REQUEST||readl(slotp(d,s,SO_TARGET_OWNER))!=d->id)return;
    mode=readl(slotp(d,s,SO_MODE));p=readl(slotp(d,s,SO_PAGE));req=readl(slotp(d,s,SO_REQUESTER));
    target=readl(slotp(d,s,SO_TARGET_OWNER));gen=readl(slotp(d,s,SO_GEN));
    if(req>=NODES||req==d->id||target!=d->id||p>=NPAGES||(mode!=MODE_READ&&mode!=MODE_WRITE)){pr_err("RKMESH_V7_SERVICE_BAD service=%u slot=%u\n",d->id,s);return;}
    writel(S_PROCESSING,slotp(d,s,SO_STATE));wmb();
    mutex_lock(&d->page_lock[p]);owner=page_owner(d,p);busy=page_busy(d,p);
    if(owner!=d->id){mutex_unlock(&d->page_lock[p]);send_retry(d,s,req,owner);return;}
    if(busy){mutex_unlock(&d->page_lock[p]);send_retry(d,s,req,owner);return;}
    memcpy_toio(transferp(d,s),local_page(d,p),PAGE_SIZE);wmb();
    if(mode==MODE_WRITE){writel(req,d->bar2+OWNER_BASE+p*4);writel(req+1,d->bar2+BUSY_BASE+p*4);}
    add_u32(slotp(d,s,SO_PAGE_XFERS),1);add_u64(slotp(d,s,SO_PAGE_BYTES),PAGE_SIZE);
    writel(d->id,slotp(d,s,SO_RESPONSE_OWNER));wmb();writel(S_RESPONSE,slotp(d,s,SO_STATE));wmb();
    mutex_unlock(&d->page_lock[p]);ring_peer(d,req);
    pr_info("RKMESH_V7_PAGE_TRANSFER mode=%s slot=%u from=%u to=%u page=%u bytes=%lu gen=%u checksum=0x%016llx\n",
            mode==MODE_READ?"READ":"WRITE",s,d->id,req,p,PAGE_SIZE,gen,(unsigned long long)checksum(local_page(d,p),PAGE_SIZE));
}

static irqreturn_t rk_irq(int irq,void*opaque)
{
    struct rkmesh_dev*d=opaque;u32 s,st,req,target;bool handled=false;int c=atomic_inc_return(&d->irq_count);
    for(s=0;s<NSLOTS;s++){
        st=readl(slotp(d,s,SO_STATE));req=readl(slotp(d,s,SO_REQUESTER));target=readl(slotp(d,s,SO_TARGET_OWNER));
        if(st==S_REQUEST&&target==d->id){schedule_work(&d->sw[s].work);handled=true;}
        if((st==S_RESPONSE||st==S_RETRY)&&req==d->id){wake_up(&d->resp_wq);handled=true;}
    }
    pr_info("RKMESH_V7_IRQ id=%u count=%d irq=%d handled=%u\n",d->id,c,irq,handled?1:0);return IRQ_HANDLED;
}

static int post_and_wait(struct rkmesh_dev*d,struct rk_xfer*x)
{
    u32 s=d->id,owner,gen,st,retries=0;int attempt;u64 start=ktime_get_ns();
    for(attempt=0;attempt<64;attempt++){
        owner=page_owner(d,x->index);if(owner==d->id)return -EALREADY;
        gen=readl(slotp(d,s,SO_GEN))+1;writel(x->mode,slotp(d,s,SO_MODE));writel(x->index,slotp(d,s,SO_PAGE));
        writel(d->id,slotp(d,s,SO_REQUESTER));writel(owner,slotp(d,s,SO_TARGET_OWNER));writel(gen,slotp(d,s,SO_GEN));
        writel(retries,slotp(d,s,SO_RETRIES));wmb();writel(S_REQUEST,slotp(d,s,SO_STATE));wmb();ring_peer(d,owner);
        if(!wait_event_timeout(d->resp_wq,((st=readl(slotp(d,s,SO_STATE)))==S_RESPONSE||st==S_RETRY)&&readl(slotp(d,s,SO_GEN))==gen,msecs_to_jiffies(4000)))return -ETIMEDOUT;
        st=readl(slotp(d,s,SO_STATE));if(st==S_RETRY){retries++;msleep(1);continue;}
        memcpy_fromio(local_page(d,x->index),transferp(d,s),PAGE_SIZE);
        if(copy_to_user((void __user*)(unsigned long)x->user_ptr,local_page(d,x->index),PAGE_SIZE))return -EFAULT;
        x->latency_ns=ktime_get_ns()-start;x->from=readl(slotp(d,s,SO_RESPONSE_OWNER));x->irq_count=atomic_read(&d->irq_count);
        x->retries=retries;x->generation=gen;writel(S_DONE,slotp(d,s,SO_STATE));wmb();return 0;
    }
    return -EAGAIN;
}

static void fill_status(struct rkmesh_dev*d,struct rk_status*st)
{
    u32 s;memset(st,0,sizeof(*st));st->id=d->id;st->step=readl(d->bar2+STEP_OFF);st->done=readl(d->bar2+DONE_OFF);
    st->irq_count=atomic_read(&d->irq_count);st->owner0=page_owner(d,0);st->owner1=page_owner(d,1);st->owner2=page_owner(d,2);
    st->owner4=page_owner(d,4);st->owner5=page_owner(d,5);st->owner6=page_owner(d,6);st->owner9=page_owner(d,9);
    st->busy0=page_busy(d,0);st->busy2=page_busy(d,2);st->busy4=page_busy(d,4);st->busy6=page_busy(d,6);
    st->local_hits0=readl(d->bar2+LOCAL_HITS_BASE);st->local_hits1=readl(d->bar2+LOCAL_HITS_BASE+4);
    st->local_hits2=readl(d->bar2+LOCAL_HITS_BASE+8);st->local_hits3=readl(d->bar2+LOCAL_HITS_BASE+12);
    st->task_target=readl(d->bar2+TASK_TARGET_OFF);st->task_scenario=readl(d->bar2+TASK_SCENARIO_OFF);
    st->final_checksum=readq(d->bar2+FINAL_OFF);
    for(s=0;s<NSLOTS;s++){st->page_xfers+=readl(slotp(d,s,SO_PAGE_XFERS));st->page_bytes+=readq(slotp(d,s,SO_PAGE_BYTES));}
}

static long rk_ioctl(struct file*file,unsigned int cmd,unsigned long arg)
{
    struct rkmesh_dev*d=gdev;struct rk_xfer x;struct rk_status st;u32 v,p,mode;int i,rc;u64 h;if(!d)return -ENODEV;
    switch(cmd){
    case RK_WAIT_READY:return wait_ready(d);
    case RK_GET_STATUS:fill_status(d,&st);return copy_to_user((void __user*)arg,&st,sizeof(st))?-EFAULT:0;
    case RK_ACQUIRE:
        if(copy_from_user(&x,(void __user*)arg,sizeof(x)))return -EFAULT;
        if((x.mode!=MODE_READ&&x.mode!=MODE_WRITE)||x.index>=NPAGES)return -EINVAL;
        if(x.mode==MODE_READ&&page_owner(d,x.index)==d->id){
            if(copy_to_user((void __user*)(unsigned long)x.user_ptr,local_page(d,x.index),PAGE_SIZE))return -EFAULT;
            x.latency_ns=0;x.from=d->id;x.irq_count=atomic_read(&d->irq_count);x.retries=0;x.generation=0;
            add_u32(d->bar2+LOCAL_HITS_BASE+d->id*4,1);wmb();pr_info("RKMESH_V7_LOCAL_READ id=%u page=%u\n",d->id,x.index);
            return copy_to_user((void __user*)arg,&x,sizeof(x))?-EFAULT:0;
        }
        rc=mutex_lock_interruptible(&d->req_lock);if(rc)return rc;rc=post_and_wait(d,&x);mutex_unlock(&d->req_lock);
        if(rc)return rc;return copy_to_user((void __user*)arg,&x,sizeof(x))?-EFAULT:0;
    case RK_COMMIT:
        if(copy_from_user(&x,(void __user*)arg,sizeof(x)))return -EFAULT;p=x.index;mode=x.mode;if(mode!=MODE_WRITE||p>=NPAGES)return -EINVAL;
        mutex_lock(&d->page_lock[p]);if(page_owner(d,p)!=d->id||page_busy(d,p)!=d->id+1){mutex_unlock(&d->page_lock[p]);return -EPERM;}
        if(copy_from_user(local_page(d,p),(void __user*)(unsigned long)x.user_ptr,PAGE_SIZE)){mutex_unlock(&d->page_lock[p]);return -EFAULT;}
        writel(0,d->bar2+BUSY_BASE+p*4);wmb();mutex_unlock(&d->page_lock[p]);pr_info("RKMESH_V7_PAGE_COMMIT id=%u page=%u checksum=0x%016llx\n",d->id,p,(unsigned long long)checksum(local_page(d,p),PAGE_SIZE));return 0;
    case RK_WAIT_STEP:if(copy_from_user(&v,(void __user*)arg,sizeof(v)))return -EFAULT;for(i=0;i<10000;i++){if(readl(d->bar2+STEP_OFF)>=v)return 0;msleep(1);}return -ETIMEDOUT;
    case RK_SET_STEP:if(copy_from_user(&v,(void __user*)arg,sizeof(v)))return -EFAULT;writel(v,d->bar2+STEP_OFF);wmb();return 0;
    case RK_WAIT_DONE:for(i=0;i<10000;i++){if(readl(d->bar2+DONE_OFF)==DONE_MAGIC)return 0;msleep(1);}return -ETIMEDOUT;
    case RK_SET_TARGET:if(d->id!=0)return -EPERM;if(copy_from_user(&v,(void __user*)arg,sizeof(v)))return -EFAULT;if(v>=NODES)return -EINVAL;writel(v+1,d->bar2+TASK_TARGET_OFF);wmb();return 0;
    case RK_SET_SCENARIO:if(d->id!=0)return -EPERM;if(copy_from_user(&v,(void __user*)arg,sizeof(v)))return -EFAULT;if(v>1)return -EINVAL;writel(v+1,d->bar2+TASK_SCENARIO_OFF);wmb();return 0;
    case RK_TASK_FINISH:if(copy_from_user(&h,(void __user*)arg,sizeof(h)))return -EFAULT;if(readl(d->bar2+TASK_TARGET_OFF)!=d->id+1)return -EPERM;writeq(h,d->bar2+FINAL_OFF);wmb();writel(DONE_MAGIC,d->bar2+DONE_OFF);wmb();return 0;
    default:return -ENOTTY;
    }
}
static const struct file_operations fops={.owner=THIS_MODULE,.unlocked_ioctl=rk_ioctl,
#ifdef CONFIG_COMPAT
.compat_ioctl=rk_ioctl,
#endif
};

static int rk_probe(struct pci_dev*pdev,const struct pci_device_id*id)
{
    struct rkmesh_dev*d;int rc,i;u32 p,s;d=devm_kzalloc(&pdev->dev,sizeof(*d),GFP_KERNEL);if(!d)return -ENOMEM;
    d->pdev=pdev;pci_set_drvdata(pdev,d);if((rc=pcim_enable_device(pdev)))return rc;pci_set_master(pdev);
    if((rc=pcim_iomap_regions(pdev,BIT(0)|BIT(2),"rkmesh_v7")))return rc;d->bar0=pcim_iomap_table(pdev)[0];d->bar2=pcim_iomap_table(pdev)[2];
    if(!d->bar0||!d->bar2)return -ENODEV;d->id=readl(d->bar0+OFF_POS);if(d->id>=NODES)return -EINVAL;
    d->pages=devm_kzalloc(&pdev->dev,(size_t)NPAGES*PAGE_SIZE,GFP_KERNEL);if(!d->pages)return -ENOMEM;
    init_waitqueue_head(&d->resp_wq);mutex_init(&d->req_lock);for(p=0;p<NPAGES;p++)mutex_init(&d->page_lock[p]);
    atomic_set(&d->irq_count,0);for(s=0;s<NSLOTS;s++){d->sw[s].d=d;d->sw[s].slot=s;INIT_WORK(&d->sw[s].work,service_slot);}
    if((rc=pci_alloc_irq_vectors(pdev,1,1,PCI_IRQ_MSIX))<0)return rc;d->irq=pci_irq_vector(pdev,0);
    if((rc=request_irq(d->irq,rk_irq,0,"rkmesh_v7",d))){pci_free_irq_vectors(pdev);return rc;}
    d->misc.minor=MISC_DYNAMIC_MINOR;d->misc.name="rkmesh_v7";d->misc.fops=&fops;gdev=d;
    if((rc=misc_register(&d->misc))){gdev=NULL;free_irq(d->irq,d);pci_free_irq_vectors(pdev);return rc;}
    if(d->id==0){
        writel(0,d->bar2+STEP_OFF);writel(0,d->bar2+DONE_OFF);writeq(0,d->bar2+FINAL_OFF);writel(0,d->bar2+TASK_TARGET_OFF);writel(0,d->bar2+TASK_SCENARIO_OFF);
        for(i=0;i<NODES;i++)writel(0,d->bar2+LOCAL_HITS_BASE+i*4);
        for(s=0;s<NSLOTS;s++){memset_io(slotp(d,s,0),0,SLOT_STRIDE);writel(s,slotp(d,s,SO_REQUESTER));}
        for(p=0;p<NPAGES;p++){writel(p%NODES,d->bar2+OWNER_BASE+p*4);writel(0,d->bar2+BUSY_BASE+p*4);init_page_bytes(local_page(d,p),p);}
        wmb();writel(INIT_MAGIC,d->bar2+INIT_OFF);wmb();
    } else {
        for(i=0;i<3000&&readl(d->bar2+INIT_OFF)!=INIT_MAGIC;i++)msleep(1);if(i==3000)return -ETIMEDOUT;
        for(p=0;p<NPAGES;p++)if(p%NODES==d->id)init_page_bytes(local_page(d,p),p);
    }
    writel(READY_MAGIC|d->id,d->bar2+READY_BASE+d->id*4);wmb();pr_info("RKMESH_V7_READY id=%u irq=%d\n",d->id,d->irq);return 0;
}
static void rk_remove(struct pci_dev*pdev){struct rkmesh_dev*d=pci_get_drvdata(pdev);u32 s;if(gdev==d)gdev=NULL;misc_deregister(&d->misc);for(s=0;s<NSLOTS;s++)cancel_work_sync(&d->sw[s].work);free_irq(d->irq,d);pci_free_irq_vectors(pdev);}
static const struct pci_device_id ids[]={{PCI_DEVICE(RK_VENDOR,RK_DEVICE)},{0,}};MODULE_DEVICE_TABLE(pci,ids);
static struct pci_driver rkmesh_v7_driver={.name="rkmesh_v7",.id_table=ids,.probe=rk_probe,.remove=rk_remove};
module_pci_driver(rkmesh_v7_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("RKMesh v7 adaptive cost-aware placement experiment");
