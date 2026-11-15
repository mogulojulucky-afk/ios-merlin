#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the X606V boot needs.

X606V = Lenovo Tab M10 FHD Plus 4G (`x606v`, MediaTek MT6762T / Helio P22T, 8 cores).

The kernel used at this stage is still built with MACHINE_CONFIG=SWEET
(the generic non-PAC arm64 bring-up from ios-sweet), so /arm-io and the
platform-expert matching contract stay the same; the device identity and
the hardware facts below are x606v's.

Facts (sources, see NOTES.md):
  - mt6762t.dts:         GIC v3 GICD@0x0c000000 (0x40000), GICR@0x0c040000
                         (0x200000, 8 PEs x 0x40000 stride), sysirq polarity
                         @0x0c53a650, arch timer (vtimer PPI 11 -> INTID 27),
                         CNTFRQ 13 MHz, PSCI smc (.arm,psci-1.0)
  - x606v panel:         TDDI IPS 1920x1200, 10.3"
  - stock boot DTB:      /memory 0x40000000 + 0x3e605000 (lk patches real DRAM
                         at boot; bundled node is the placeholder)

Usage:  mkadt.py OUT.adt [--dump] [--qemu] [--phone] [--qemu-gic]
  --qemu : QEMU virt twin (tools/qemu-x606v.py): QEMU's GIC/CNTFRQ.
  --qemu-gic: with --phone, keep the phone RAM band and panel but use
          QEMU's GIC/timer/CPU tree, so the exact phone payload can boot
          in qemu-shim past pe_arm_init_interrupts (the phone GIC at
          0x0c000000/0x0c040000 is unmapped there).  Not a hardware
          image - a bring-up test image.
  --phone: x606v MMIO + phone RAM band (provisional until the live lk FDT
           has been read on device; the shim prints it: see NOTES.md).
Default = --qemu (that is what boots today).
"""
import hashlib, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

# ---- QEMU twin (virt, gic-version=3, -cpu cortex-a75) -----------------------
QEMU_PHYS_BASE = 0xb0000000          # same band the SWEET bugfixes tuned
QEMU_MEM_SIZE = 0x4d000000
QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE = 0x08000000, 0x080a0000, 62500000
QEMU_VTIMER_INTID = 27               # standard PPI 11

# ---- phone (x606v / MT6762T) ------------------------------------------------
PHONE_PHYS_BASE = 0x80000000         # clear of the LK splash FB (ends ~0x7FDED000);
                                     # 0xB0000000 proved unwritable on the phone
PHONE_MEM_SIZE = 0x4d000000          # top 0xCD000000.  XNU's bootstrap tables cover
                                     # the 0xFFFFFE000B700000 IO window only when
                                     # physBase+memSize >= ~0xC40E4000 (verified in
                                     # qemu-shim: 0xB0+0x100 and 0x80+0x1C panic in
                                     # pmap_map_bd, 0xA0+0x4D boots to userspace).
GICD, GICD_SIZE = 0x0c000000, 0x40000
GICR, GICR_SIZE = 0x0c040000, 0x200000  # mt6762t.dts reg[1]; 8 PEs x 0x40000
SOC_BASE = 0x01000000                # /arm-io children are relative to this
TIMEBASE = 13000000                  # mt6762t arch timer CNTFRQ
VTIMER_INTID = 27                    # standard (PPI 11)
PANEL_W, PANEL_H = 1920, 1200        # TDDI IPS 1920x1200 (x606v)
# lk splash framebuffer ("atag,videolfb"): sits at the top of the bundled
# /memory chunk, below the kernel band above.  MTK lk bakes RGB888 there.
FB, FB_SIZE = 0x7e605000, 0x017e8000
# eMMC 5.1 on MSDC0 (mt6762t.dts msdc@11230000).  Emitted for the real phone;
# read by the /x606v-msdc block driver (xnu/patches/0080).
MSDC0, MSDC0_SIZE = 0x11230000, 0x10000

X606V_ECID = 0x0000583630365600      # 'X606V\0\0' synthetic (not the phone)

BOOT_MANIFEST_HASH = hashlib.sha384(b'x606v boot manifest').digest()

# The iOS userland is the iPhone X (iPhone10,3 / D22AP) IPSW, exactly like
# ios-sweet.  MobileGestalt derives product type, device class, home-button-less
# UI, artwork etc. from the root identity and /product of the real D22AP device
# tree.  That tree is an Apple artifact (kept out of git):
#   tools/remotezip.py URL DeviceTree.d22ap.im4p DIR
#   ipsw img4 im4p extract DIR/Firmware/all_flash/DeviceTree.d22ap.im4p -o d22ap.adt
D22AP_ADT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../research/ipsw-x/d22ap.adt')


def d22ap_product(path=D22AP_ADT):
    """/product properties of the real D22AP tree, without the syscfg
    placeholders iBoot would fill in (flag bit set) and the node bookkeeping."""
    if not os.path.exists(path):
        print(f'warning: {path} missing, no /product node', file=sys.stderr)
        return None
    root, _ = adt.parse(open(path, 'rb').read())
    for c in root.children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')


u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'
iph = lambda core: u32(1 << 8 | core)  # MT6762T MPIDR Aff1:Aff0 compact


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n


def build(qemu=True, phone=False, phys_base=None, mem_size=None):
    """--qemu/--phone choose the MMIO/RAM band; the identity is always x606v
    with the iPhone X userland compatibility (D22AP /product when present).
    phys_base/mem_size override the band (to test a relocated payload)."""
    if phys_base is None:
        phys_base = PHONE_PHYS_BASE if phone else QEMU_PHYS_BASE
    if mem_size is None:
        mem_size = PHONE_MEM_SIZE if phone else QEMU_MEM_SIZE
    gicd, gicr, timebase = (QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE) if qemu else (GICD, GICR, TIMEBASE)
    vtimer = QEMU_VTIMER_INTID if qemu else VTIMER_INTID
    if qemu and not phone:
        # QEMU virt has no ABL splash: keep the console plane at the same
        # address/geometry the SWEET qemu twin used (qemu-x606v.py saves it).
        fb, fb_w, fb_h = 0x9c000000, 1080, 2400
        fb_size = 0x1700000
    else:
        # --phone (also with --qemu-gic): the real x606v panel geometry.
        fb, fb_w, fb_h = FB, PANEL_W, PANEL_H
        fb_size = fb_h * (fb_w * 4)
    if phone and not qemu:
        # The panel scans each row starting 270 px into the framebuffer row
        # (quadrant probe, 2026-10-23); console rows start 1080 bytes in.
        fb += 270 * 4
    seed = os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(phys_base)),
        ('dram-size', u64(mem_size)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('firmware-version', cstr('x606v-shim')),
        ('system-firmware-version', cstr('x606v-shim')),
        # iBoot's SHA-384 of the boot manifest; /sbin/mount -P 1 exits unless
        # it exists.  Fixed stand-in value (see ios-sweet NOTES).
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        ('crypto-hash-method', cstr('sha2-384')),
        # MobileGestalt's screen class: iPhone X canvas @3x like sweet.
        ('display-scale', u32(3)),
        # XNU derives IOPlatformUUID (gethostuuid) from these on arm.
        ('unique-chip-id', struct.pack('<Q', X606V_ECID)),
        # A11 chip-id: what the iPhone X userland expects.
        ('chip-id', u32(0x8015)),
        # Dev-fused, undemoted security status -> mobileactivationd Hactivation.
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
    ], [node('memory-map', [ph()]),
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))])])

    # MT6762T / Helio P22T (stock DTB /cpus): cores 0-3 are Cortex-A53
    # (clock-frequency 0x89388000 = 2.3 GHz), cores 4-7 are Cortex-A53
    # (clock-frequency 0x6A09E680 = 1.8 GHz).  Stock MPIDRs are sequential:
    # 0x000,0x100,...,0x700 (cpu@000..003 = cluster0, cpu@100..103 = cluster1).
    # QEMU keeps its single-CPU tree.
    cores = range(1 if qemu else 8)
    freq = lambda core: 0x89388000 if core < 4 else 0x6A09E680
    # compatible[0] MUST stay 'qcom,kryo470': the SWEET kernel's boot-CPU
    # driver personality matches IONameMatch 'qcom,kryo470'
    # (xnu/patches/0018 SweetCPU via KernelConfigTables).  'ARM,cortex-a53'
    # never matches -> SweetCPU::start never runs -> no ml_processor_register
    # -> gIOAllCPUInitializedKey never publishes -> kernel_bootstrap waits
    # forever after 'kprintf startup complete' (found via gdb cdp dump:
    # cpu_id/cluster_master unset).  MT6762T cores 0-3 are really A53 @2.3,
    # 4-7 A53 @1.8 -- recorded here only, the compatible is a driver key.
    compat = lambda core: cstr('qcom,kryo470') + cstr('ARM,v8')
    cpu_nodes = []
    for core in cores:
        cpu_nodes.append(node(f'cpu{core}', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', compat(core)),
            ('reg', u32(core << 8)),          # actual stock MPIDR
            ('cpu-id', u32(core)),
            ('cluster-id', u32(0 if core < 4 else 1)),
            ('cluster-type', u32(0)),         # homogeneous CLUSTER_TYPE_SMP
            ('die-cluster-id', u32(0 if core < 4 else 1)),
            ('cluster-core-id', u32(core % 4)),
            ('l2-cache-id', u32(core)),
            ('l3-cache-id', u32(0)),
            ('state', cstr('running' if core == 0 else 'stopped')),
            ('timebase-frequency', u32(timebase)),
            ('clock-frequency', u32(freq(core))),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1800000000)),
            ('peripheral-frequency', u32(timebase)),
            ('fixed-frequency', u32(timebase)),
        ]))
    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], cpu_nodes)

    gic = node('gic', [
        ph(),
        ('compatible', cstr('arm,gic-v3') + cstr('mediatek,gic500')),
        ('interrupt-controller', b''),
        ('virtual-timer-intid', u32(vtimer)),
        # Sizes differ per host: QEMU virt needs the ios-sweet-proven windows
        # (the kernel probes redistributor frames inside the mapped GICR
        # window; a single 0x20000-frame map livelocks early interrupts).
        # Phone keeps the MT6762T GIC-500 layout (GICD block 0x40000 per
        # mt6762t.dts; GICR 8 PEs * 0x40000 == 0x200000, live stride TBD).
        ('reg', u64(gicd - SOC_BASE, 0x10000 if qemu else GICD_SIZE,
                    gicr - SOC_BASE, 0x100000 if qemu else GICR_SIZE)),
    ])
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('x606v-io')),
        ('compatible', cstr('x606v-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [gic,
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        # Main display for IOMobileFramebuffer/AppleCLCD (SweetFramebuffer):
        # canvas is the presented iPhone X size; the x606v panel itself is
        # 1920x1200 TDDI IPS (lk splash keeps it alive for bring-up).
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('sweet,display')),
            ('canvas-width', u32(1125)),
            ('canvas-height', u32(2436)),
            ('panel-width', u32(PANEL_W)),
            ('panel-height', u32(PANEL_H)),
        ])])

    # Kernel-interface name: the patched fbcon driver IONameMatches the ADT
    # node 'sweet-fbcon' (x606v identity lives elsewhere).  A renamed node
    # boots fine but the console goes invisible after the early FB console,
    # as proven by RAM forensics (research/abtest fbcon variant).
    fbcon = node('sweet-fbcon', [
        ph(),
        ('reg', u64(fb, fb_size)),
        ('width', u32(fb_w)),
        ('height', u32(fb_h)),
        ('stride', u32(fb_w * 4)),
    ])

    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])

    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    product_props = d22ap_product()
    product = [node('product', [ph()] + product_props)] if product_props else []
    msdc = [node('x606v-msdc', [ph(), ('reg', u64(MSDC0, MSDC0_SIZE))])] \
        if (phone and not qemu) else []

    # hw.model/hw.target = D22AP, hw.machine/hw.product = iPhone10,3.
    # 'lenovo,x606v' stays FIRST of the machine-identities so the x606v
    # platform expert keeps matching until its own machine config + MTK drivers.
    return node('device-tree', [
        ph(),
        ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr('lenovo,x606v')
                     + cstr('xiaomi,sweet') + cstr('mediatek,mt6762') + cstr('AppleARM')),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons] + msdc + product)


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        raise SystemExit(__doc__)
    phys = mem = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == '--phys-base':
            phys = int(args[i + 1], 0); i += 2
        elif args[i] == '--mem-size':
            mem = int(args[i + 1], 0); i += 2
        else:
            rest.append(args[i]); i += 1
    args = rest
    qemu = ('--qemu-gic' in args) or not ('--phone' in args)
    root = build(qemu=qemu, phone='--phone' in args,
                 phys_base=phys, mem_size=mem)
    blob = adt.serialize(root)
    target = args[0]
    if target == '-':
        sys.stdout.buffer.write(blob)
    else:
        open(target, 'wb').write(blob)
    if '--dump' in args:
        adt.dump(adt.parse(blob)[0])
    print(f'{target}: {len(blob)} bytes', file=sys.stderr)   
