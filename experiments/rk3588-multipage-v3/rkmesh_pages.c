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

#define RK_VENDOR 0x1af4
#define RK_DEVICE 0x1110
#define OFF_POS 8
#define OFF_DOORBELL 12
#define NODES 4
#define NPAGES 16
#define LEASE_BYTES (4 * PAGE_SIZE)
#define READY_BASE 0x000
#define INIT_OFF 0x020
#define STEP_OFF 0x024
#define DONE_OFF 0x028
#define FINAL_OFF 0x030
#define PAGE_BYTES_OFF 0x038
#define LEASE_BYTES_OFF 0x040
#define IRQ_TOTAL_OFF 0x048
#define INVALIDATIONS_OFF 0x04c
#define PAGE_XFERS_OFF 0x050
#define LEASE_XFERS_OFF 0x054
#define OWNER_BASE 0x100
#define READERS_BASE 0x140
#define SUM_BASE 0x200
#define MSG_TYPE_OFF 0x300
#define MSG_PAGE_OFF 0x304
#define MSG_REQUESTER_OFF 0x308
#define MSG_MODE_OFF 0x30c
#define MSG_TARGET_OFF 0x310
#define MSG_ACK_OFF 0x314
#define MSG_SEQ_OFF 0x318
#define LEASE_OWNER_OFF 0x340
#define LEASE_SUM_OFF 0x348
#define TRANSFER_OFF 0x1000
#define LEASE_TRANSFER_OFF 0x20000
#define READY_MAGIC 0x35880000U
#define INIT_MAGIC 0x4d503003U
#define DONE_MAGIC 0x600df003U
#define MSG_IDLE 0U
#define MSG_REQ 1U
#define MSG_RESP 2U
#define MSG_INV 3U
#define MODE_READ 1U
#define MODE_WRITE 2U
#define MODE_LEASE 3U

#define RK_IOC_MAGIC 'M'
struct rk_xfer { __u64 user_ptr; __u64 latency_ns; __u32 index; __u32 mode; __u32 from; __u32 irq_count; };
struct rk_status { __u32 id, step, done, irq_count; __u32 owner0, owner1, readers0, readers1; __u32 invalidations, page_xfers, lease_xfers, lease_owner; __u64 page_bytes, lease_bytes, final_checksum, lease_checksum; };
#define RK_WAIT_READY _IO(RK_IOC_MAGIC,1)
#define RK_GET_STATUS _IOR(RK_IOC_MAGIC,2,struct rk_status)
#define RK_ACQUIRE _IOWR(RK_IOC_MAGIC,3,struct rk_xfer)
#define RK_COMMIT _IOW(RK_IOC_MAGIC,4,struct rk_xfer)
#define RK_WAIT_INVALIDATE _IOR(RK_IOC_MAGIC,5,__u32)
#define RK_ACK_INVALIDATE _IOW(RK_IOC_MAGIC,6,__u32)
#define RK_WAIT_STEP _IOW(RK_IOC_MAGIC,7,__u32)
#define RK_SET_STEP _IOW(RK_IOC_MAGIC,8,__u32)
#define RK_FINALIZE _IO(RK_IOC_MAGIC,9)
#define RK_WAIT_DONE _IO(RK_IOC_MAGIC,10)

struct rkmesh_dev {
    struct pci_dev *pdev;
    void __iomem *bar0, *bar2;
    u32 id;
    int irq;
    atomic_t irq_count;
    wait_queue_head_t resp_wq, inv_wq, ack_wq;
    struct work_struct service_work;
    struct mutex req_lock;
    u8 *pages;
    u8 *lease;
    int pending_inv_page;
    struct miscdevice misc;
};
static struct rkmesh_dev *gdev;

static inline void ring_peer(struct rkmesh_dev *d, u32 peer) { writel(peer << 16, d->bar0 + OFF_DOORBELL); }
static inline u32 page_owner(struct rkmesh_dev *d,u32 p){ return readl(d->bar2+OWNER_BASE+p*4); }
static inline u32 page_readers(struct rkmesh_dev *d,u32 p){ return readl(d->bar2+READERS_BASE+p*4); }
static inline u8 *local_page(struct rkmesh_dev *d,u32 p){ return d->pages + (size_t)p*PAGE_SIZE; }
static inline void add_u32(struct rkmesh_dev *d,u32 off,u32 v){ writel(readl(d->bar2+off)+v,d->bar2+off); }
static inline void add_u64(struct rkmesh_dev *d,u32 off,u64 v){ writeq(readq(d->bar2+off)+v,d->bar2+off); }

static u64 checksum(const u8 *p,size_t n){ u64 h=0xcbf29ce484222325ULL; size_t i; for(i=0;i<n;i++){h^=p[i];h*=0x100000001b3ULL;} return h; }
static void init_page_bytes(u8 *p,u32 pg){ size_t i; for(i=0;i<PAGE_SIZE;i++)p[i]=(u8)((i*37U+11U+pg*13U)&0xffU); ((u64*)p)[0]=0x3588358835883588ULL ^ ((u64)pg*0x0101010101010101ULL); }
static void init_lease_bytes(u8 *p){ size_t i; for(i=0;i<LEASE_BYTES;i++)p[i]=(u8)((i*19U+7U)&0xffU); ((u64*)p)[0]=0x1ea51ea51ea51ea5ULL; }
static bool all_ready(struct rkmesh_dev*d){u32 i;for(i=0;i<NODES;i++)if(readl(d->bar2+READY_BASE+i*4)!=(READY_MAGIC|i))return false;return true;}
static int wait_ready(struct rkmesh_dev*d){int i;for(i=0;i<5000;i++){if(all_ready(d))return 0;msleep(1);}return -ETIMEDOUT;}
static int wait_idle(struct rkmesh_dev*d){int i;for(i=0;i<5000;i++){if(readl(d->bar2+MSG_TYPE_OFF)==MSG_IDLE)return 0;msleep(1);}return -ETIMEDOUT;}

static void respond(struct rkmesh_dev*d,u32 requester){ wmb(); writel(MSG_RESP,d->bar2+MSG_TYPE_OFF); wmb(); ring_peer(d,requester); }

static void service_workfn(struct work_struct *work)
{
    struct rkmesh_dev*d=container_of(work,struct rkmesh_dev,service_work);
    u32 type=readl(d->bar2+MSG_TYPE_OFF), mode=readl(d->bar2+MSG_MODE_OFF);
    u32 p=readl(d->bar2+MSG_PAGE_OFF), req=readl(d->bar2+MSG_REQUESTER_OFF);
    u32 owner, readers, targets, i;
    if(type!=MSG_REQ || req>=NODES || req==d->id){pr_err("RKMESH_V3_SERVICE_FAIL id=%u type=%u req=%u\n",d->id,type,req);return;}
    if(mode==MODE_LEASE){
        owner=readl(d->bar2+LEASE_OWNER_OFF);
        if(owner!=d->id){pr_err("RKMESH_V3_LEASE_OWNER_FAIL id=%u owner=%u\n",d->id,owner);return;}
        memcpy_toio(d->bar2+LEASE_TRANSFER_OFF,d->lease,LEASE_BYTES); wmb();
        writel(req,d->bar2+LEASE_OWNER_OFF); add_u64(d,LEASE_BYTES_OFF,LEASE_BYTES); add_u32(d,LEASE_XFERS_OFF,1);
        pr_info("RKMESH_V3_LEASE_TRANSFER from=%u to=%u bytes=%lu checksum=0x%016llx\n",d->id,req,(unsigned long)LEASE_BYTES,(unsigned long long)checksum(d->lease,LEASE_BYTES));
        respond(d,req); return;
    }
    if(p>=NPAGES){pr_err("RKMESH_V3_PAGE_FAIL id=%u page=%u\n",d->id,p);return;}
    owner=page_owner(d,p); readers=page_readers(d,p);
    if(owner!=d->id){pr_err("RKMESH_V3_OWNER_FAIL id=%u page=%u owner=%u\n",d->id,p,owner);return;}
    if(mode==MODE_WRITE){
        targets=readers & ~(1U<<req);
        if(targets){
            writel(targets,d->bar2+MSG_TARGET_OFF); writel(0,d->bar2+MSG_ACK_OFF); writel(MSG_INV,d->bar2+MSG_TYPE_OFF); wmb();
            for(i=0;i<NODES;i++)if(targets&(1U<<i)){ring_peer(d,i); add_u32(d,INVALIDATIONS_OFF,1); pr_info("RKMESH_V3_INVALIDATE_SEND owner=%u page=%u target=%u\n",d->id,p,i);}
            if(!wait_event_timeout(d->ack_wq,(readl(d->bar2+MSG_ACK_OFF)&targets)==targets,msecs_to_jiffies(3000))){pr_err("RKMESH_V3_INVALIDATE_TIMEOUT owner=%u page=%u targets=0x%x ack=0x%x\n",d->id,p,targets,readl(d->bar2+MSG_ACK_OFF));return;}
            pr_info("RKMESH_V3_INVALIDATE_ALL_ACK owner=%u page=%u mask=0x%x\n",d->id,p,targets);
        }
    }
    memcpy_toio(d->bar2+TRANSFER_OFF,local_page(d,p),PAGE_SIZE); wmb();
    add_u64(d,PAGE_BYTES_OFF,PAGE_SIZE); add_u32(d,PAGE_XFERS_OFF,1);
    if(mode==MODE_READ) writel(readers|(1U<<req),d->bar2+READERS_BASE+p*4);
    else { writel(req,d->bar2+OWNER_BASE+p*4); writel(0,d->bar2+READERS_BASE+p*4); }
    pr_info("RKMESH_V3_PAGE_TRANSFER mode=%s from=%u to=%u page=%u bytes=%lu checksum=0x%016llx readers_before=0x%x\n",mode==MODE_READ?"READ":"WRITE",d->id,req,p,PAGE_SIZE,(unsigned long long)checksum(local_page(d,p),PAGE_SIZE),readers);
    respond(d,req);
}

static irqreturn_t rkmesh_irq(int irq,void*opaque)
{
    struct rkmesh_dev*d=opaque; u32 type,req,p,targets,owner; int c=atomic_inc_return(&d->irq_count);
    add_u32(d,IRQ_TOTAL_OFF,1); type=readl(d->bar2+MSG_TYPE_OFF); req=readl(d->bar2+MSG_REQUESTER_OFF); p=readl(d->bar2+MSG_PAGE_OFF); targets=readl(d->bar2+MSG_TARGET_OFF);
    pr_info("RKMESH_V3_IRQ id=%u count=%d irq=%d type=%u page=%u req=%u\n",d->id,c,irq,type,p,req);
    if(type==MSG_REQ){
        owner=readl(d->bar2+MSG_MODE_OFF)==MODE_LEASE?readl(d->bar2+LEASE_OWNER_OFF):(p<NPAGES?page_owner(d,p):99);
        if(owner==d->id){schedule_work(&d->service_work);return IRQ_HANDLED;}
    }
    if(type==MSG_RESP && req==d->id){wake_up(&d->resp_wq);return IRQ_HANDLED;}
    if(type==MSG_INV){
        owner=(p<NPAGES)?page_owner(d,p):99;
        if((targets&(1U<<d->id)) && d->id!=owner){d->pending_inv_page=(int)p;wake_up(&d->inv_wq);return IRQ_HANDLED;}
        if(d->id==owner){wake_up(&d->ack_wq);return IRQ_HANDLED;}
    }
    pr_err("RKMESH_V3_IRQ_UNEXPECTED id=%u type=%u page=%u req=%u targets=0x%x\n",d->id,type,p,req,targets); return IRQ_HANDLED;
}

static long rk_ioctl(struct file*file,unsigned int cmd,unsigned long arg)
{
    struct rkmesh_dev*d=gdev; struct rk_xfer x; struct rk_status st; u32 v,p,mode,owner,ack; int rc=0,i; u64 start,h;
    if(!d)return -ENODEV;
    switch(cmd){
    case RK_WAIT_READY:return wait_ready(d);
    case RK_GET_STATUS:
        memset(&st,0,sizeof(st)); st.id=d->id;st.step=readl(d->bar2+STEP_OFF);st.done=readl(d->bar2+DONE_OFF);st.irq_count=atomic_read(&d->irq_count);st.owner0=page_owner(d,0);st.owner1=page_owner(d,1);st.readers0=page_readers(d,0);st.readers1=page_readers(d,1);st.invalidations=readl(d->bar2+INVALIDATIONS_OFF);st.page_xfers=readl(d->bar2+PAGE_XFERS_OFF);st.lease_xfers=readl(d->bar2+LEASE_XFERS_OFF);st.lease_owner=readl(d->bar2+LEASE_OWNER_OFF);st.page_bytes=readq(d->bar2+PAGE_BYTES_OFF);st.lease_bytes=readq(d->bar2+LEASE_BYTES_OFF);st.final_checksum=readq(d->bar2+FINAL_OFF);st.lease_checksum=readq(d->bar2+LEASE_SUM_OFF);return copy_to_user((void __user*)arg,&st,sizeof(st))?-EFAULT:0;
    case RK_ACQUIRE:
        if(copy_from_user(&x,(void __user*)arg,sizeof(x)))return -EFAULT; p=x.index;mode=x.mode;if((mode!=MODE_READ&&mode!=MODE_WRITE&&mode!=MODE_LEASE)||(mode!=MODE_LEASE&&p>=NPAGES))return -EINVAL;
        rc=mutex_lock_interruptible(&d->req_lock);if(rc)return rc; if((rc=wait_ready(d))||(rc=wait_idle(d)))goto out;
        owner=mode==MODE_LEASE?readl(d->bar2+LEASE_OWNER_OFF):page_owner(d,p); if(owner==d->id){rc=-EALREADY;goto out;}
        start=ktime_get_ns(); writel(p,d->bar2+MSG_PAGE_OFF);writel(d->id,d->bar2+MSG_REQUESTER_OFF);writel(mode,d->bar2+MSG_MODE_OFF);writel(0,d->bar2+MSG_TARGET_OFF);writel(0,d->bar2+MSG_ACK_OFF);writel(readl(d->bar2+MSG_SEQ_OFF)+1,d->bar2+MSG_SEQ_OFF);wmb();writel(MSG_REQ,d->bar2+MSG_TYPE_OFF);wmb();ring_peer(d,owner);pr_info("RKMESH_V3_REQUEST mode=%u requester=%u owner=%u index=%u\n",mode,d->id,owner,p);
        if(!wait_event_timeout(d->resp_wq,readl(d->bar2+MSG_TYPE_OFF)==MSG_RESP&&readl(d->bar2+MSG_REQUESTER_OFF)==d->id,msecs_to_jiffies(3000))){rc=-ETIMEDOUT;goto out;}
        if(mode==MODE_LEASE){memcpy_fromio(d->lease,d->bar2+LEASE_TRANSFER_OFF,LEASE_BYTES);if(copy_to_user((void __user*)(unsigned long)x.user_ptr,d->lease,LEASE_BYTES)){rc=-EFAULT;goto out;}}
        else {memcpy_fromio(local_page(d,p),d->bar2+TRANSFER_OFF,PAGE_SIZE);if(copy_to_user((void __user*)(unsigned long)x.user_ptr,local_page(d,p),PAGE_SIZE)){rc=-EFAULT;goto out;}}
        x.latency_ns=ktime_get_ns()-start;x.from=owner;x.irq_count=atomic_read(&d->irq_count);writel(MSG_IDLE,d->bar2+MSG_TYPE_OFF);wmb();pr_info("RKMESH_V3_ACQUIRE_DONE mode=%u id=%u from=%u index=%u latency_ns=%llu\n",mode,d->id,owner,p,(unsigned long long)x.latency_ns);if(copy_to_user((void __user*)arg,&x,sizeof(x)))rc=-EFAULT;
    out:mutex_unlock(&d->req_lock);return rc;
    case RK_COMMIT:
        if(copy_from_user(&x,(void __user*)arg,sizeof(x)))return -EFAULT;p=x.index;mode=x.mode;
        if(mode==MODE_LEASE){if(readl(d->bar2+LEASE_OWNER_OFF)!=d->id)return -EPERM;if(copy_from_user(d->lease,(void __user*)(unsigned long)x.user_ptr,LEASE_BYTES))return -EFAULT;h=checksum(d->lease,LEASE_BYTES);writeq(h,d->bar2+LEASE_SUM_OFF);pr_info("RKMESH_V3_LEASE_COMMIT id=%u checksum=0x%016llx\n",d->id,(unsigned long long)h);return 0;}
        if(mode!=MODE_WRITE||p>=NPAGES||page_owner(d,p)!=d->id)return -EPERM;if(copy_from_user(local_page(d,p),(void __user*)(unsigned long)x.user_ptr,PAGE_SIZE))return -EFAULT;h=checksum(local_page(d,p),PAGE_SIZE);writeq(h,d->bar2+SUM_BASE+p*8);pr_info("RKMESH_V3_PAGE_COMMIT id=%u page=%u checksum=0x%016llx\n",d->id,p,(unsigned long long)h);return 0;
    case RK_WAIT_INVALIDATE:
        if(!wait_event_timeout(d->inv_wq,d->pending_inv_page>=0,msecs_to_jiffies(6000)))return -ETIMEDOUT;v=(u32)d->pending_inv_page;d->pending_inv_page=-1;return copy_to_user((void __user*)arg,&v,sizeof(v))?-EFAULT:0;
    case RK_ACK_INVALIDATE:
        if(copy_from_user(&v,(void __user*)arg,sizeof(v)))return -EFAULT;if(v>=NPAGES||readl(d->bar2+MSG_TYPE_OFF)!=MSG_INV||readl(d->bar2+MSG_PAGE_OFF)!=v)return -EINVAL;ack=readl(d->bar2+MSG_ACK_OFF)|(1U<<d->id);writel(ack,d->bar2+MSG_ACK_OFF);wmb();owner=page_owner(d,v);ring_peer(d,owner);pr_info("RKMESH_V3_INVALIDATE_ACK id=%u page=%u owner=%u ack=0x%x\n",d->id,v,owner,ack);return 0;
    case RK_WAIT_STEP:
        if(copy_from_user(&v,(void __user*)arg,sizeof(v)))return -EFAULT;for(i=0;i<10000;i++){if(readl(d->bar2+STEP_OFF)==v)return 0;msleep(1);}return -ETIMEDOUT;
    case RK_SET_STEP:
        if(copy_from_user(&v,(void __user*)arg,sizeof(v)))return -EFAULT;writel(v,d->bar2+STEP_OFF);wmb();pr_info("RKMESH_V3_STEP id=%u step=%u\n",d->id,v);return 0;
    case RK_FINALIZE:
        if(d->id!=3)return -EPERM;h=0xcbf29ce484222325ULL;for(p=0;p<NPAGES;p++){u64 s=readq(d->bar2+SUM_BASE+p*8);int b;for(b=0;b<8;b++){h^=(u8)(s>>(b*8));h*=0x100000001b3ULL;}}{u64 s=readq(d->bar2+LEASE_SUM_OFF);int b;for(b=0;b<8;b++){h^=(u8)(s>>(b*8));h*=0x100000001b3ULL;}}writeq(h,d->bar2+FINAL_OFF);writel(DONE_MAGIC,d->bar2+DONE_OFF);wmb();pr_info("RKMESH_V3_FINAL checksum=0x%016llx page_bytes=%llu lease_bytes=%llu invalidations=%u irq_total=%u\n",(unsigned long long)h,(unsigned long long)readq(d->bar2+PAGE_BYTES_OFF),(unsigned long long)readq(d->bar2+LEASE_BYTES_OFF),readl(d->bar2+INVALIDATIONS_OFF),readl(d->bar2+IRQ_TOTAL_OFF));return 0;
    case RK_WAIT_DONE:for(i=0;i<10000;i++){if(readl(d->bar2+DONE_OFF)==DONE_MAGIC)return 0;msleep(1);}return -ETIMEDOUT;
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
    struct rkmesh_dev*d;int rc,i;u32 p;u64 h;
    d=devm_kzalloc(&pdev->dev,sizeof(*d),GFP_KERNEL);if(!d)return -ENOMEM;d->pdev=pdev;pci_set_drvdata(pdev,d);
    if((rc=pcim_enable_device(pdev)))return rc;pci_set_master(pdev);if((rc=pcim_iomap_regions(pdev,BIT(0)|BIT(2),"rkmesh_pages")))return rc;d->bar0=pcim_iomap_table(pdev)[0];d->bar2=pcim_iomap_table(pdev)[2];if(!d->bar0||!d->bar2)return -ENODEV;d->id=readl(d->bar0+OFF_POS);if(d->id>=NODES)return -EINVAL;
    d->pages=devm_kzalloc(&pdev->dev,(size_t)NPAGES*PAGE_SIZE,GFP_KERNEL);d->lease=devm_kzalloc(&pdev->dev,LEASE_BYTES,GFP_KERNEL);if(!d->pages||!d->lease)return -ENOMEM;init_waitqueue_head(&d->resp_wq);init_waitqueue_head(&d->inv_wq);init_waitqueue_head(&d->ack_wq);INIT_WORK(&d->service_work,service_workfn);mutex_init(&d->req_lock);d->pending_inv_page=-1;atomic_set(&d->irq_count,0);
    if((rc=pci_alloc_irq_vectors(pdev,1,1,PCI_IRQ_MSIX))<0)return rc;d->irq=pci_irq_vector(pdev,0);if((rc=request_irq(d->irq,rkmesh_irq,0,"rkmesh_pages",d))){pci_free_irq_vectors(pdev);return rc;}
    d->misc.minor=MISC_DYNAMIC_MINOR;d->misc.name="rkmesh_pages";d->misc.fops=&fops;gdev=d;if((rc=misc_register(&d->misc))){gdev=NULL;free_irq(d->irq,d);pci_free_irq_vectors(pdev);return rc;}
    if(d->id==0){writel(0,d->bar2+STEP_OFF);writel(0,d->bar2+DONE_OFF);writeq(0,d->bar2+FINAL_OFF);writeq(0,d->bar2+PAGE_BYTES_OFF);writeq(0,d->bar2+LEASE_BYTES_OFF);writel(0,d->bar2+IRQ_TOTAL_OFF);writel(0,d->bar2+INVALIDATIONS_OFF);writel(0,d->bar2+PAGE_XFERS_OFF);writel(0,d->bar2+LEASE_XFERS_OFF);writel(MSG_IDLE,d->bar2+MSG_TYPE_OFF);writel(0,d->bar2+MSG_SEQ_OFF);writel(0,d->bar2+LEASE_OWNER_OFF);for(p=0;p<NPAGES;p++){writel(p%NODES,d->bar2+OWNER_BASE+p*4);writel(0,d->bar2+READERS_BASE+p*4);init_page_bytes(local_page(d,p),p);h=checksum(local_page(d,p),PAGE_SIZE);writeq(h,d->bar2+SUM_BASE+p*8);}init_lease_bytes(d->lease);writeq(checksum(d->lease,LEASE_BYTES),d->bar2+LEASE_SUM_OFF);wmb();writel(INIT_MAGIC,d->bar2+INIT_OFF);wmb();}
    else {for(i=0;i<2000&&readl(d->bar2+INIT_OFF)!=INIT_MAGIC;i++)msleep(1);if(i==2000)return -ETIMEDOUT;for(p=0;p<NPAGES;p++)if(p%NODES==d->id)init_page_bytes(local_page(d,p),p);}
    if(d->id==0)init_lease_bytes(d->lease);
    writel(READY_MAGIC|d->id,d->bar2+READY_BASE+d->id*4);wmb();pr_info("RKMESH_V3_READY id=%u irq=%d\n",d->id,d->irq);return 0;
}
static void rk_remove(struct pci_dev*pdev){struct rkmesh_dev*d=pci_get_drvdata(pdev);if(gdev==d)gdev=NULL;misc_deregister(&d->misc);cancel_work_sync(&d->service_work);free_irq(d->irq,d);pci_free_irq_vectors(pdev);}
static const struct pci_device_id ids[]={{PCI_DEVICE(RK_VENDOR,RK_DEVICE)},{0,}};MODULE_DEVICE_TABLE(pci,ids);
static struct pci_driver drv={.name="rkmesh_pages",.id_table=ids,.probe=rk_probe,.remove=rk_remove};module_pci_driver(drv);MODULE_LICENSE("GPL");MODULE_DESCRIPTION("RKMesh multi-page ownership, replicas, invalidation, and lease v3");
