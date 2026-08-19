# RK3568 PCIe documentation source note

Added 2026-08-19 as hardware-enablement evidence for the RK3588 multi-SoC compute-fabric project.

## Source repository

- Repository: `yunzhaoyu2050/rockchip_rk3568_docs`
- Source URL: https://github.com/yunzhaoyu2050/rockchip_rk3568_docs
- Repository document index: `docs_list.txt`
- Relevant directory: `Common/PCIe/`

Observed PCIe documents:

- `Common/PCIe/Rockchip_RK356X_Developer_Guide_PCIe_CN.pdf`
  - repository blob SHA: `f278b8e87e8ef8c170dd297a64cced6c961f35f6`
  - size: 450234 bytes
- `Common/PCIe/Rockchip_Developer_Guide_Linux4.4_PCIe_CN.pdf`
  - repository blob SHA: `a5c2c1e808e74b72f92c022a6723a561175d5ba3`
  - size: 421493 bytes

The RK356X-specific guide is the useful source here; it should be mined for Rockchip PCIe RC/EP configuration, device-tree patterns, PHY/reset/clock sequencing, address translation, BAR handling, DMA and interrupt test procedures. Do not transfer register values or board-level assumptions from RK3568 to RK3588 without RK3588-specific corroboration.

## RK3588 corroboration from upstream Linux

Current upstream Linux contains explicit RK3588 endpoint-mode support in:

`drivers/pci/controller/dwc/pcie-dw-rockchip.c`

Relevant implementation facts in the upstream driver:

- OF compatible `rockchip,rk3588-pcie-ep` is present and selects `DW_PCIE_EP_TYPE`.
- Endpoint configuration switches the Rockchip client block to `PCIE_CLIENT_MODE_EP`.
- The endpoint path calls `dw_pcie_ep_init()` and `dw_pcie_ep_init_registers()`.
- The endpoint device uses `dma_set_mask_and_coherent(..., DMA_BIT_MASK(64))`.
- The endpoint IRQ implementation supports INTx, MSI and MSI-X.
- RK3588 endpoint features expose resizable BAR0-3 and BAR5; BAR4 is reserved because RK3588 exposes DMA/ATU port-logic structures there.
- The driver explicitly hides ATS capability in RK3588 EP mode because ATS/IOTLB invalidation completion is broken in this mode; ATS must therefore not be assumed usable for the planned fabric.

Upstream binding documentation also identifies RK3588 RC support as inheriting from the RK3568 DesignWare PCIe binding lineage via `rockchip,rk3588-pcie` + `rockchip,rk3568-pcie`.

Primary upstream references:

- https://github.com/torvalds/linux/blob/master/drivers/pci/controller/dwc/pcie-dw-rockchip.c
- https://github.com/torvalds/linux/blob/master/Documentation/devicetree/bindings/pci/rockchip-dw-pcie.yaml

## Project consequence

This evidence narrows the real-silicon uncertainty. It is no longer appropriate to frame RK3588 endpoint mode itself as merely inferred from RK3568. Upstream Linux contains an explicit RK3588 DesignWare endpoint implementation.

Still UNPROVEN for this project until executed on hardware:

1. two real RK3588 boards linked RC <-> EP;
2. successful LTSSM/link training and endpoint enumeration;
3. project-controlled BAR exposure and read/write semantics;
4. useful bidirectional DMA behavior and measured throughput/latency;
5. MSI/MSI-X delivery under the intended runtime protocol;
6. cache-maintenance/coherency discipline for shared execution objects;
7. behavior through the eventual external PCIe switch topology.

Smallest hardware discriminator remains two boards only: establish RC<->EP, enumerate, expose one BAR, move controlled data, raise/receive an interrupt, then measure DMA and coherency behavior. Do not treat QEMU ARM `virt` evidence as RK3588 silicon proof.
