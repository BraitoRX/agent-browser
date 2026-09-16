# GPU in the bubble: research and PoC notes

Status as of 2026-09-15. Goal: keep the os-native input isolation (Xvfb + XTEST inside a private display) while giving the bubble's browser access to the host Apple Silicon GPU. Everything here was verified against live sources; links are the durable record.

## Why the current bubble has no GPU

OrbStack, Docker Desktop, and Apple's `container` all build on `Virtualization.framework`, which exposes no GPU to Linux guests (no Metal, no Vulkan, no VA-API). The Apple GPU has no passthrough interfaces on Apple Silicon, and Apple has shown no sign of changing this: `apple/container` issue #1511 (GPU) is open without commitment; earlier issues were closed without a plan. The feature request to OrbStack for libkrun is `orbstack/discussions/1408` (41 thumbs, no official reply since 2024-08).

The only Linux-guest GPU path on Apple Silicon is libkrun/krunkit, which implements its own virtio-gpu device on top of `Hypervisor.framework`: guest Mesa Venus/virgl forwards Vulkan/GL calls to the host, where virglrenderer/MoltenVK translate them to Metal. Red Hat measured roughly 75 to 77 percent of native Metal performance for Vulkan compute (llama.cpp), with minimal forwarding overhead.

## What the PoC established on this machine (M4, macOS 27)

- Podman 6.1.1 (brew) plus krunkit 1.3.2 boots a libkrun VM successfully.
- The krunkit shipped in the brew tap (`slp/krunkit`, version 1.1.1) is stale; Podman 6.1.1 passes `--timesync vsockPort=...` which 1.1.1 rejects, and 1.3.2 accepts. Use krunkit 1.3.2 from `libkrun/krunkit` releases (`krunkit-podman-unsigned` bundle).
- The unsigned bundle needs ad-hoc re-signing of krunkit and its dylibs, plus the `com.apple.security.hypervisor` and `com.apple.security.cs.disable-library-validation` entitlements from the repo's `krunkit.entitlements`, or `VmCreate` fails under Hypervisor.framework.
- krunkit resolves its EFI firmware (`share/krunkit/KRUN_EFI.silent.fd`) relative to the binary's own directory; a symlinked binary breaks the relative lookup ("can't find a firmware to load"). Keep the binary beside its `share/` copy.
- libkrun exposes `/dev/dri/card0` and `/dev/dri/renderD128` in the guest; the `virtio_gpu` driver binds (virtio device type 0x0010), and `/sys/class/dmi/id/sys_vendor` reports `Libkrun`.
- Devices pass into containers (`podman run --device /dev/dri`), appearing with `nobody` ownership under rootless mapping; the render node is usable by the container.
- Guest page size is 4096 and host mappings require 16 KiB alignment: stock Debian/Ubuntu Mesa Venus fails with `VK_ERROR_OUT_OF_HOST_MEMORY` (krunkit #114). Fedora ships a patched Mesa, or apply slp's 16 KiB alignment patch to Mesa in the guest. This is the documented reason llama.cpp setups use Fedora images.

## The one real blocker: VA-API video decode

The browser's dominant CPU cost is video decode (YouTube H.264/VP9), not compositing. Findings:

- VirGL video acceleration (`VIRGL_RENDERER_USE_VIDEO`) exists upstream in virglrenderer but requires the VMM to pass the flag at `virgl_renderer_init()` time. QEMU upstream has never merged it (work item qemu-project#2196, stalled since 2024).
- Proxmox carries a downstream QEMU patch (tested through `pve-qemu 10.1.2-7`) enabling the DRM fd callback plus the video flag; users report H.264/HEVC profiles appearing in guest `vainfo` with it.
- Ubuntu bug #2141694 (updated 2026-08) documents a working Ubuntu 26.04 VirGL reproducer where GNOME Remote Desktop is smooth but `vainfo` shows only `VAProfileNone` without the QEMU flag; the reporter plans test builds and an upstream patch. Watch this bug.
- Venus forwards Vulkan only; there is no video path through Venus.
- libkrun does not drive QEMU's virglrenderer path, so even the Proxmox patch does not apply to krunkit as-is. Nothing in the krunkit/libkrun issue tracker currently exposes `VIRGL_RENDERER_USE_VIDEO`.

## Community evidence consulted (live checks, September 2026)

- `orbstack/discussions/1408`: GPU/libkrun request, no official commitment.
- `podman-container-tools/podman/discussions/25999`: user-confirmed GPU in containers via libkrun with `--device /dev/dri`, visible in macOS Activity Monitor; requires patched Mesa guest.
- `libkrun/krunkit#114`: 16 KiB blob mapping failure breaking stock Mesa Venus on 4 KiB guests.
- Launchpad qemu #2141694 and virglrenderer #2141692: the VA-API forwarding plumbing; upstream QEMU #2196 is the stalled gate.
- UTM issue reports (2023 to 2026): Firefox hardware acceleration works under virtio-gpu/virgl (virgl, ANGLE, Metal renderer string in glxinfo); browser GPU support regresses with version churn (Chromium lost it in March 2026; QEMU 10 regressed virgl to OpenGL 2.1 in UTM #7691).
- No public report of a GUI browser running GPU-accelerated inside libkrun/krunkit specifically; documented use is Vulkan compute (llama.cpp, RamaLama).

## PoC result and implications

The libkrun path works on this Mac end to end for Vulkan forwarding: the VM boots (Podman 6.1.1 machine with provider `libkrun`, krunkit 1.3.2), `/dev/dri/renderD128` exists in the guest, devices pass into containers, and `vulkaninfo --summary` inside a container reports `Virtio-GPU Venus (Apple M4)` as an integrated physical device through Mesa 25.3.6 Venus. The forwarding chain container, Venus, virtio-gpu, libkrun, MoltenVK, Metal is live.

Practical constraints confirmed during the run:

- Stock Fedora 41 Mesa fails with `VK_ERROR_OUT_OF_HOST_MEMORY` in `vkCreateInstance` (krunkit #114); the RamaLama image (`quay.io/ramalama/ramalama`), which carries the patched Mesa, enumerates the GPU successfully.
- The RamaLama image also attempts the Asahi native context path and fails on `card0` permissions; Venus still works because the container only needs `renderD128`. Granting `card0` access would be a further experiment, not a requirement for Vulkan compute.
- Nested virtualization (`--nested`) is passed by Podman 6.1.1 on this M4 and works.
- A long image pull through the nested container stack can stall the VM's network responsiveness; run image pulls before starting latency-sensitive work.

Even with Vulkan forwarding confirmed, a browser-based bubble migration would additionally require:

1. Replacing Xvfb with Xorg modesetting over the render node, since Xvfb never consumes a GPU regardless of host capabilities.
2. A guest image with Mesa Venus patched for 16 KiB alignment (Fedora-based, or patched Debian).
3. Daemon changes: `docker run` becomes `podman run` with `--device /dev/dri`, plus losing OrbStack container domains (`orb.local`) and the fixed noVNC domain built earlier.
4. Accepting that video decode stays on CPU unless the QEMU/virgl video flag work lands in libkrun.

Decision rule adopted: keep the OrbStack bubble as the daily driver; revisit a libkrun migration when the QEMU video flag lands upstream or in a krunkit release. For video-heavy viewing on the same machine, run a second, native Camoufox session (GPU plus VideoToolbox decode, no input isolation) alongside the bubble.