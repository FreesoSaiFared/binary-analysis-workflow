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
#define READY_BASE 0x000
#define OWNER_OFF 0x040
#define STATE_OFF 0x044
#define REQUESTER_OFF 0x048
#define SEQ_OFF 0x04c
#define DONE_OFF 0x050
#define FINAL_SUM_OFF 0x058
#define TRANSFER_OFF 0x1000
#define READY_MAGIC 0x35880000U
#define DONE_MAGIC 0x600df00dU
#define STATE_IDLE 0U
#define STATE_REQ 1U
#define STATE_RESP 2U

#define RK_IOC_MAGIC 'R'
struct rk_user_xfer { __u64 user_ptr; __u64 latency_ns; __u32 from; __u32 irq_count; };
struct rk_status { __u32 id; __u32 owner; __u32 state; __u32 irq_count; __u32 done; __u32 reserved; __u64 final_checksum; };
#define RK_IOCTL_WAIT_READY _IO(RK_IOC_MAGIC, 1)
#define RK_IOCTL_GET_STATUS _IOR(RK_IOC_MAGIC, 2, struct rk_status)
#define RK_IOCTL_ACQUIRE _IOWR(RK_IOC_MAGIC, 3, struct rk_user_xfer)
#define RK_IOCTL_COMMIT _IOW(RK_IOC_MAGIC, 4, struct rk_user_xfer)
#define RK_IOCTL_WAIT_DONE _IO(RK_IOC_MAGIC, 5)

struct rkmesh_dev {
    struct pci_dev *pdev;
    void __iomem *bar0;
    void __iomem *bar2;
    u32 id;
    int irq;
    atomic_t irq_count;
    wait_queue_head_t resp_wq;
    struct work_struct service_work;
    struct mutex ioctl_lock;
    u8 *local_page;
    struct miscdevice misc;
};

static struct rkmesh_dev *gdev;

static inline void ring_peer(struct rkmesh_dev *d, u32 peer)
{
    writel(peer << 16, d->bar0 + OFF_DOORBELL);
}

static bool all_ready(struct rkmesh_dev *d)
{
    u32 i;
    for (i = 0; i < 4; i++)
        if (readl(d->bar2 + READY_BASE + i * 4) != (READY_MAGIC | i))
            return false;
    return true;
}

static int wait_all_ready(struct rkmesh_dev *d)
{
    int i;
    for (i = 0; i < 5000; i++) {
        if (all_ready(d))
            return 0;
        msleep(1);
    }
    return -ETIMEDOUT;
}

static u64 checksum_page(const u8 *p)
{
    u64 h = 0xcbf29ce484222325ULL;
    size_t i;
    for (i = 0; i < PAGE_SIZE; i++) {
        h ^= p[i];
        h *= 0x100000001b3ULL;
    }
    return h;
}

static void init_page(u8 *p)
{
    size_t i;
    for (i = 0; i < PAGE_SIZE; i++)
        p[i] = (u8)((i * 37U + 11U) & 0xffU);
    ((u64 *)p)[0] = 0x3588358835883588ULL;
}

static void service_workfn(struct work_struct *work)
{
    struct rkmesh_dev *d = container_of(work, struct rkmesh_dev, service_work);
    u32 state = readl(d->bar2 + STATE_OFF);
    u32 owner = readl(d->bar2 + OWNER_OFF);
    u32 requester = readl(d->bar2 + REQUESTER_OFF);
    u64 sum;

    if (state != STATE_REQ || owner != d->id || requester > 3 || requester == d->id) {
        pr_err("RKMESH_V2_SERVICE_FAIL id=%u state=%u owner=%u requester=%u\n",
               d->id, state, owner, requester);
        return;
    }

    sum = checksum_page(d->local_page);
    memcpy_toio(d->bar2 + TRANSFER_OFF, d->local_page, PAGE_SIZE);
    wmb();
    writel(requester, d->bar2 + OWNER_OFF);
    writel(STATE_RESP, d->bar2 + STATE_OFF);
    wmb();
    pr_info("RKMESH_V2_PAGE_TRANSFER from=%u to=%u bytes=%lu checksum=0x%016llx\n",
            d->id, requester, PAGE_SIZE, (unsigned long long)sum);
    ring_peer(d, requester);
    pr_info("RKMESH_V2_RESPONSE_DOORBELL from=%u to=%u\n", d->id, requester);
}

static irqreturn_t rkmesh_irq(int irq, void *opaque)
{
    struct rkmesh_dev *d = opaque;
    int count = atomic_inc_return(&d->irq_count);
    u32 state = readl(d->bar2 + STATE_OFF);
    u32 owner = readl(d->bar2 + OWNER_OFF);
    u32 requester = readl(d->bar2 + REQUESTER_OFF);

    pr_info("RKMESH_V2_IRQ id=%u count=%d irq=%d state=%u owner=%u requester=%u\n",
            d->id, count, irq, state, owner, requester);

    if (state == STATE_REQ && owner == d->id) {
        pr_info("RKMESH_V2_REQUEST_IRQ owner=%u requester=%u\n", d->id, requester);
        schedule_work(&d->service_work);
        return IRQ_HANDLED;
    }

    if (state == STATE_RESP && owner == d->id && requester == d->id) {
        pr_info("RKMESH_V2_RESPONSE_IRQ requester=%u\n", d->id);
        wake_up(&d->resp_wq);
        return IRQ_HANDLED;
    }

    pr_err("RKMESH_V2_IRQ_UNEXPECTED id=%u state=%u owner=%u requester=%u\n",
           d->id, state, owner, requester);
    return IRQ_HANDLED;
}

static long rkmesh_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct rkmesh_dev *d = gdev;
    struct rk_user_xfer x;
    struct rk_status st;
    u32 owner, state;
    int i, rc = 0;
    u64 start;

    if (!d)
        return -ENODEV;

    switch (cmd) {
    case RK_IOCTL_WAIT_READY:
        return wait_all_ready(d);

    case RK_IOCTL_GET_STATUS:
        memset(&st, 0, sizeof(st));
        st.id = d->id;
        st.owner = readl(d->bar2 + OWNER_OFF);
        st.state = readl(d->bar2 + STATE_OFF);
        st.irq_count = atomic_read(&d->irq_count);
        st.done = readl(d->bar2 + DONE_OFF);
        st.final_checksum = readq(d->bar2 + FINAL_SUM_OFF);
        return copy_to_user((void __user *)arg, &st, sizeof(st)) ? -EFAULT : 0;

    case RK_IOCTL_ACQUIRE:
        if (d->id == 0)
            return -EINVAL;
        if (copy_from_user(&x, (void __user *)arg, sizeof(x)))
            return -EFAULT;
        rc = mutex_lock_interruptible(&d->ioctl_lock);
        if (rc)
            return rc;
        rc = wait_all_ready(d);
        if (rc)
            goto out_unlock;

        for (i = 0; i < 5000; i++) {
            owner = readl(d->bar2 + OWNER_OFF);
            state = readl(d->bar2 + STATE_OFF);
            if (owner == d->id - 1 && state == STATE_IDLE)
                break;
            msleep(1);
        }
        if (i == 5000) {
            rc = -ETIMEDOUT;
            goto out_unlock;
        }

        start = ktime_get_ns();
        writel(d->id, d->bar2 + REQUESTER_OFF);
        writel(readl(d->bar2 + SEQ_OFF) + 1, d->bar2 + SEQ_OFF);
        wmb();
        writel(STATE_REQ, d->bar2 + STATE_OFF);
        wmb();
        pr_info("RKMESH_V2_REQUEST_DOORBELL requester=%u owner=%u\n", d->id, owner);
        ring_peer(d, owner);

        if (!wait_event_timeout(d->resp_wq,
                readl(d->bar2 + STATE_OFF) == STATE_RESP &&
                readl(d->bar2 + OWNER_OFF) == d->id,
                msecs_to_jiffies(3000))) {
            rc = -ETIMEDOUT;
            goto out_unlock;
        }

        memcpy_fromio(d->local_page, d->bar2 + TRANSFER_OFF, PAGE_SIZE);
        if (copy_to_user((void __user *)(unsigned long)x.user_ptr, d->local_page, PAGE_SIZE)) {
            rc = -EFAULT;
            goto out_unlock;
        }
        x.latency_ns = ktime_get_ns() - start;
        x.from = owner;
        x.irq_count = atomic_read(&d->irq_count);
        pr_info("RKMESH_V2_ACQUIRE_COMPLETE id=%u from=%u latency_ns=%llu checksum=0x%016llx\n",
                d->id, owner, (unsigned long long)x.latency_ns,
                (unsigned long long)checksum_page(d->local_page));
        if (copy_to_user((void __user *)arg, &x, sizeof(x)))
            rc = -EFAULT;
out_unlock:
        mutex_unlock(&d->ioctl_lock);
        return rc;

    case RK_IOCTL_COMMIT:
        if (copy_from_user(&x, (void __user *)arg, sizeof(x)))
            return -EFAULT;
        if (readl(d->bar2 + OWNER_OFF) != d->id)
            return -EPERM;
        if (copy_from_user(d->local_page, (void __user *)(unsigned long)x.user_ptr, PAGE_SIZE))
            return -EFAULT;
        writel(STATE_IDLE, d->bar2 + STATE_OFF);
        wmb();
        pr_info("RKMESH_V2_COMMIT id=%u checksum=0x%016llx\n",
                d->id, (unsigned long long)checksum_page(d->local_page));
        if (d->id == 3) {
            writeq(checksum_page(d->local_page), d->bar2 + FINAL_SUM_OFF);
            writel(DONE_MAGIC, d->bar2 + DONE_OFF);
            wmb();
            pr_info("RKMESH_V2_CHAIN_DONE owner=3 final_checksum=0x%016llx\n",
                    (unsigned long long)checksum_page(d->local_page));
        }
        return 0;

    case RK_IOCTL_WAIT_DONE:
        for (i = 0; i < 5000; i++) {
            if (readl(d->bar2 + DONE_OFF) == DONE_MAGIC)
                return 0;
            msleep(1);
        }
        return -ETIMEDOUT;

    default:
        return -ENOTTY;
    }
}

static const struct file_operations rkmesh_fops = {
    .owner = THIS_MODULE,
    .unlocked_ioctl = rkmesh_ioctl,
#ifdef CONFIG_COMPAT
    .compat_ioctl = rkmesh_ioctl,
#endif
};

static int rkmesh_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
    struct rkmesh_dev *d;
    int rc;

    d = devm_kzalloc(&pdev->dev, sizeof(*d), GFP_KERNEL);
    if (!d)
        return -ENOMEM;
    d->pdev = pdev;
    pci_set_drvdata(pdev, d);

    rc = pcim_enable_device(pdev);
    if (rc)
        return rc;
    pci_set_master(pdev);
    rc = pcim_iomap_regions(pdev, BIT(0) | BIT(2), "rkmesh_page");
    if (rc)
        return rc;
    d->bar0 = pcim_iomap_table(pdev)[0];
    d->bar2 = pcim_iomap_table(pdev)[2];
    if (!d->bar0 || !d->bar2)
        return -ENODEV;
    d->id = readl(d->bar0 + OFF_POS);
    if (d->id > 3)
        return -EINVAL;

    d->local_page = devm_kzalloc(&pdev->dev, PAGE_SIZE, GFP_KERNEL);
    if (!d->local_page)
        return -ENOMEM;
    init_waitqueue_head(&d->resp_wq);
    INIT_WORK(&d->service_work, service_workfn);
    mutex_init(&d->ioctl_lock);
    atomic_set(&d->irq_count, 0);

    rc = pci_alloc_irq_vectors(pdev, 1, 1, PCI_IRQ_MSIX);
    if (rc < 0)
        return rc;
    d->irq = pci_irq_vector(pdev, 0);
    rc = request_irq(d->irq, rkmesh_irq, 0, "rkmesh_page", d);
    if (rc) {
        pci_free_irq_vectors(pdev);
        return rc;
    }

    d->misc.minor = MISC_DYNAMIC_MINOR;
    d->misc.name = "rkmesh_page";
    d->misc.fops = &rkmesh_fops;
    gdev = d;
    rc = misc_register(&d->misc);
    if (rc) {
        gdev = NULL;
        free_irq(d->irq, d);
        pci_free_irq_vectors(pdev);
        return rc;
    }

    if (d->id == 0) {
        init_page(d->local_page);
        writel(0, d->bar2 + REQUESTER_OFF);
        writel(0, d->bar2 + SEQ_OFF);
        writel(0, d->bar2 + DONE_OFF);
        writeq(0, d->bar2 + FINAL_SUM_OFF);
        writel(0, d->bar2 + OWNER_OFF);
        writel(STATE_IDLE, d->bar2 + STATE_OFF);
        wmb();
        pr_info("RKMESH_V2_INITIAL_OWNER id=0 checksum=0x%016llx\n",
                (unsigned long long)checksum_page(d->local_page));
    }

    writel(READY_MAGIC | d->id, d->bar2 + READY_BASE + d->id * 4);
    wmb();
    pr_info("RKMESH_V2_READY id=%u irq=%d\n", d->id, d->irq);
    return 0;
}

static void rkmesh_remove(struct pci_dev *pdev)
{
    struct rkmesh_dev *d = pci_get_drvdata(pdev);
    if (gdev == d)
        gdev = NULL;
    misc_deregister(&d->misc);
    cancel_work_sync(&d->service_work);
    free_irq(d->irq, d);
    pci_free_irq_vectors(pdev);
}

static const struct pci_device_id ids[] = {
    { PCI_DEVICE(RK_VENDOR, RK_DEVICE) },
    { 0, }
};
MODULE_DEVICE_TABLE(pci, ids);

static struct pci_driver rkmesh_page_driver = {
    .name = "rkmesh_page",
    .id_table = ids,
    .probe = rkmesh_probe,
    .remove = rkmesh_remove,
};
module_pci_driver(rkmesh_page_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("RKMesh fault-driven page acquisition over ivshmem MSI-X v2");
