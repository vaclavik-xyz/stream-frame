// vrcap — Vulkan implicit layer: mirrors the frames the compositor
// presents into a POSIX shared-memory ring. It hooks vkQueuePresentKHR, blits the
// presented swapchain image (scaled down on the GPU) into a linear image, copies that
// into a host-visible buffer and publishes it as one frame in a 3-slot shm ring.
// Reading is asynchronous: the compositor never blocks; when the previous frame is
// still in flight on the GPU, a single frame is dropped.
//
// Env:
//   VRSTREAM_CAP=1              enable capture ("1" = on, anything else = off)
//   VRSTREAM_CAP_W=1440         target width, output height derived from aspect
//   VRSTREAM_CAP_FPS=60         frame rate cap
//   VRSTREAM_CAP_SHM=/vrstream_frames   shm ring name (POSIX shm_open)
//   VRSTREAM_CAP_MAX=0          stop after N frames (0 = unlimited)
#include <stdio.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>

#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>

#define LOGP "/tmp/vrstream-layer.log"
#define MAX_SC 8
#define SLOTS 3

typedef struct {
    VkDevice dev;
    PFN_vkGetDeviceProcAddr gdpa;
    PFN_vkCreateImage pCreateImage;
    PFN_vkGetImageMemoryRequirements pGetImageMemoryRequirements;
    PFN_vkAllocateMemory pAllocateMemory;
    PFN_vkBindImageMemory pBindImageMemory;
    PFN_vkCreateBuffer pCreateBuffer;
    PFN_vkGetBufferMemoryRequirements pGetBufferMemoryRequirements;
    PFN_vkBindBufferMemory pBindBufferMemory;
    PFN_vkCreateCommandPool pCreateCommandPool;
    PFN_vkAllocateCommandBuffers pAllocateCommandBuffers;
    PFN_vkBeginCommandBuffer pBeginCommandBuffer;
    PFN_vkEndCommandBuffer pEndCommandBuffer;
    PFN_vkCmdPipelineBarrier pCmdPipelineBarrier;
    PFN_vkCmdBlitImage pCmdBlitImage;
    PFN_vkCmdCopyImageToBuffer pCmdCopyImageToBuffer;
    PFN_vkQueueSubmit pQueueSubmit;
    PFN_vkCreateFence pCreateFence;
    PFN_vkGetFenceStatus pGetFenceStatus;
    PFN_vkWaitForFences pWaitForFences;
    PFN_vkResetFences pResetFences;
    PFN_vkMapMemory pMapMemory;
    PFN_vkGetPhysicalDeviceMemoryProperties pGetPhysMemProps;
    VkPhysicalDevice phys;
    VkQueue queue;
    uint32_t queueFamily;

    VkImage lin;
    VkDeviceMemory linMem;
    VkBuffer buf;
    VkDeviceMemory bufMem;
    void *mapped;
    VkCommandPool pool;
    VkCommandBuffer cmd;
    VkFence fence;
    uint32_t srcW, srcH, dstW, dstH;
    VkFormat format;
    int pending;          // copy submitted, waiting for completion
    unsigned long frames, dropped;
    int max_frames;
} CapCtx;

typedef struct { VkSwapchainKHR sc; VkFormat format; VkExtent2D extent; uint32_t count; VkImage images[8]; } ScInfo;

static PFN_vkGetInstanceProcAddr g_gipa;
static PFN_vkGetDeviceProcAddr g_gdpa;
static PFN_vkCreateDevice g_create_device;
static PFN_vkQueuePresentKHR g_present;
static PFN_vkCreateSwapchainKHR g_create_sc;
static PFN_vkGetSwapchainImagesKHR g_get_sc_images;
static PFN_vkDestroySwapchainKHR g_destroy_sc;
static VkInstance g_instance = VK_NULL_HANDLE;
static CapCtx g_cap;
static ScInfo g_scs[MAX_SC];
static int g_sc_count;
static double g_last_frame_t;
static int g_enabled = -1;
static int g_shm_fd = -1;
static unsigned char *g_shm = NULL;
static size_t g_shm_size = 0;
static uint64_t g_seq = 0;
static int g_shm_w = 0, g_shm_h = 0;

static void logmsg(const char *fmt, ...) {
    FILE *f = fopen(LOGP, "a");
    if (!f) return;
    va_list ap; va_start(ap, fmt); vfprintf(f, fmt, ap); va_end(ap);
    fputc('\n', f); fclose(f);
}

static double now_s(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static int env_int(const char *n, int def) {
    const char *v = getenv(n);
    return v ? atoi(v) : def;
}

static uint32_t mem_type_for(CapCtx *c, uint32_t bits, VkMemoryPropertyFlags want) {
    if (!c->pGetPhysMemProps || c->phys == VK_NULL_HANDLE) return UINT32_MAX;
    VkPhysicalDeviceMemoryProperties mp;
    c->pGetPhysMemProps(c->phys, &mp);
    for (uint32_t i = 0; i < mp.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want) return i;
    return UINT32_MAX;
}

static int shm_open_ring(int w, int h) {
    const char *name = getenv("VRSTREAM_CAP_SHM");
    if (!name) name = "/vrstream_frames";
    size_t pixels = (size_t)w * h * 4;
    size_t need = 64 + (size_t)SLOTS * (sizeof(uint64_t) * 2 + pixels);
    if (g_shm && g_shm_size >= need && g_shm_w == w && g_shm_h == h) return 1;

    if (g_shm) { munmap(g_shm, g_shm_size); g_shm = NULL; close(g_shm_fd); g_shm_fd = -1; }
    shm_unlink(name);
    g_shm_fd = shm_open(name, O_CREAT | O_RDWR, 0666);
    if (g_shm_fd < 0) { logmsg("shm_open failed"); return 0; }
    if (ftruncate(g_shm_fd, (off_t)need) != 0) { logmsg("ftruncate failed"); return 0; }
    void *p = mmap(NULL, need, PROT_READ | PROT_WRITE, MAP_SHARED, g_shm_fd, 0);
    if (p == MAP_FAILED) { logmsg("mmap failed"); return 0; }
    g_shm = (unsigned char *)p;
    g_shm_size = need;
    g_shm_w = w; g_shm_h = h;
    // header: [w][h][slot_size][active_slot], then per slot [seq][guard] + data
    memcpy(g_shm, &w, 4);
    memcpy(g_shm + 4, &h, 4);
    uint32_t slot_size = (uint32_t)(sizeof(uint64_t) * 2 + pixels);
    memcpy(g_shm + 8, &slot_size, 4);
    uint32_t zero = 0;
    memcpy(g_shm + 12, &zero, 4);
    for (int i = 0; i < SLOTS; ++i) {
        uint64_t z = 0;
        memcpy(g_shm + 64 + (size_t)i * slot_size, &z, 8);
    }
    logmsg("shm ready: %s %dx%d, slot %u B", name, w, h, slot_size);
    return 1;
}

static void shm_write(const void *data, size_t bytes, int w, int h) {
    if (!g_shm || g_shm_w != w || g_shm_h != h) return;
    uint32_t slot_size = 0;
    memcpy(&slot_size, g_shm + 8, 4);
    uint32_t slot = (uint32_t)(g_seq % SLOTS);
    unsigned char *base = g_shm + 64 + (size_t)slot * slot_size;
    memcpy(base + 16, data, bytes);
    g_seq++;
    __atomic_store_n((uint64_t *)base, g_seq, __ATOMIC_RELEASE);
}

static int cap_setup(CapCtx *c, uint32_t srcW, uint32_t srcH, VkFormat fmt) {
    int wantW = env_int("VRSTREAM_CAP_W", 1440);
    if (wantW < 320) wantW = 320;
    uint32_t dstW = (uint32_t)wantW;
    uint32_t dstH = (uint32_t)((uint64_t)srcH * dstW / srcW);
    dstH &= ~1u;  // keep height even

    if (c->lin && (c->srcW != srcW || c->srcH != srcH || c->dstW != dstW || c->dstH != dstH || c->format != fmt)) {
        logmsg("capture: resize %ux%u -> %ux%u, rebuilding", c->dstW, c->dstH, dstW, dstH);
        c->lin = VK_NULL_HANDLE; c->linMem = VK_NULL_HANDLE;
        c->buf = VK_NULL_HANDLE; c->bufMem = VK_NULL_HANDLE; c->mapped = NULL;
        c->cmd = VK_NULL_HANDLE; c->fence = VK_NULL_HANDLE; c->pending = 0;
    }
    c->srcW = srcW; c->srcH = srcH; c->dstW = dstW; c->dstH = dstH; c->format = fmt;
    if (c->lin) return 1;

    VkImageCreateInfo ici = {0};
    ici.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    ici.imageType = VK_IMAGE_TYPE_2D;
    ici.format = fmt;
    ici.extent.width = dstW; ici.extent.height = dstH; ici.extent.depth = 1;
    ici.mipLevels = 1; ici.arrayLayers = 1;
    ici.samples = VK_SAMPLE_COUNT_1_BIT;
    ici.tiling = VK_IMAGE_TILING_LINEAR;
    ici.usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    ici.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    if (c->pCreateImage(c->dev, &ici, NULL, &c->lin) != VK_SUCCESS) { logmsg("capture: createImage failed"); return 0; }

    VkMemoryRequirements mr; c->pGetImageMemoryRequirements(c->dev, c->lin, &mr);
    VkMemoryAllocateInfo mai = {0};
    mai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    mai.allocationSize = mr.size;
    mai.memoryTypeIndex = mem_type_for(c, mr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
    if (mai.memoryTypeIndex == UINT32_MAX || c->pAllocateMemory(c->dev, &mai, NULL, &c->linMem) != VK_SUCCESS) {
        logmsg("capture: image memory allocation failed"); return 0;
    }
    c->pBindImageMemory(c->dev, c->lin, c->linMem, 0);

    VkBufferCreateInfo bci = {0};
    bci.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bci.size = (VkDeviceSize)dstW * dstH * 4;
    bci.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    if (c->pCreateBuffer(c->dev, &bci, NULL, &c->buf) != VK_SUCCESS) { logmsg("capture: createBuffer failed"); return 0; }
    VkMemoryRequirements bmr; c->pGetBufferMemoryRequirements(c->dev, c->buf, &bmr);
    VkMemoryAllocateInfo bmai = {0};
    bmai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    bmai.allocationSize = bmr.size;
    bmai.memoryTypeIndex = mem_type_for(c, bmr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    if (bmai.memoryTypeIndex == UINT32_MAX || c->pAllocateMemory(c->dev, &bmai, NULL, &c->bufMem) != VK_SUCCESS) {
        logmsg("capture: buffer memory allocation failed"); return 0;
    }
    c->pBindBufferMemory(c->dev, c->buf, c->bufMem, 0);
    if (c->pMapMemory(c->dev, c->bufMem, 0, VK_WHOLE_SIZE, 0, &c->mapped) != VK_SUCCESS) {
        logmsg("capture: map failed"); return 0;
    }

    VkCommandPoolCreateInfo pci = {0};
    pci.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pci.queueFamilyIndex = c->queueFamily;
    if (!c->pCreateCommandPool || c->pCreateCommandPool(c->dev, &pci, NULL, &c->pool) != VK_SUCCESS) {
        logmsg("capture: pool creation failed"); return 0;
    }
    VkCommandBufferAllocateInfo cai = {0};
    cai.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    cai.commandPool = c->pool;
    cai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    cai.commandBufferCount = 1;
    if (c->pAllocateCommandBuffers(c->dev, &cai, &c->cmd) != VK_SUCCESS) { logmsg("capture: command buffer allocation failed"); return 0; }

    VkFenceCreateInfo fci = {0};
    fci.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    fci.flags = VK_FENCE_CREATE_SIGNALED_BIT;
    if (c->pCreateFence(c->dev, &fci, NULL, &c->fence) != VK_SUCCESS) { logmsg("capture: fence creation failed"); return 0; }

    if (!shm_open_ring(dstW, dstH)) { logmsg("capture: shm setup failed"); return 0; }
    logmsg("capture: ready %ux%u -> %ux%u, format %d", srcW, srcH, dstW, dstH, (int)fmt);
    return 1;
}

// finish the previous copy if it has completed; writes the data into shm
static void cap_collect(CapCtx *c) {
    static int wait_presents;
    if (!c->pending || !c->fence) return;
    if (c->pGetFenceStatus(c->dev, c->fence) != VK_SUCCESS) {
        c->dropped++;
        if (++wait_presents > 120) {
            logmsg("capture: fence stuck, resetting");
            c->pWaitForFences(c->dev, 1, &c->fence, VK_TRUE, 200000000ull);
            c->pending = 0;
            wait_presents = 0;
        }
        return;
    }
    wait_presents = 0;
    shm_write(c->mapped, (size_t)c->dstW * c->dstH * 4, (int)c->dstW, (int)c->dstH);
    c->frames++;
    c->pending = 0;
    if (c->frames % 300 == 0)
        logmsg("capture: %lu frames, %lu dropped", c->frames, c->dropped);
    if (c->max_frames > 0 && (int)c->frames >= c->max_frames) {
        logmsg("capture: reached limit of %d frames", c->frames);
        g_enabled = 0;
    }
}

static void cap_grab(CapCtx *c, const ScInfo *sc, uint32_t imageIndex) {
    if (!sc || imageIndex >= sc->count) return;
    if (!cap_setup(c, sc->extent.width, sc->extent.height, sc->format)) return;
    if (c->pending) return;  // previous frame still in flight -> drop this one

    VkImage src = sc->images[imageIndex];
    c->pResetFences(c->dev, 1, &c->fence);

    VkCommandBufferBeginInfo bi = {0};
    bi.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (c->pBeginCommandBuffer(c->cmd, &bi) != VK_SUCCESS) return;

    VkImageMemoryBarrier b1 = {0};
    b1.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    b1.oldLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    b1.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    b1.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b1.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b1.image = src;
    b1.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    b1.subresourceRange.levelCount = 1;
    b1.subresourceRange.layerCount = 1;
    b1.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    c->pCmdPipelineBarrier(c->cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                           0, 0, NULL, 0, NULL, 1, &b1);

    VkImageMemoryBarrier b2 = {0};
    b2.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    b2.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    b2.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    b2.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b2.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b2.image = c->lin;
    b2.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    b2.subresourceRange.levelCount = 1;
    b2.subresourceRange.layerCount = 1;
    b2.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    c->pCmdPipelineBarrier(c->cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                           0, 0, NULL, 0, NULL, 1, &b2);

    // The compositor delivers the image pre-rotated by 180 degrees. We flip each
    // eye separately so the stereo order stays correct (left eye on the left).
    for (int eye = 0; eye < 2; ++eye) {
        VkImageBlit region = {0};
        region.srcSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        region.srcSubresource.layerCount = 1;
        region.srcOffsets[0].x = (int32_t)(eye * c->srcW / 2);
        region.srcOffsets[0].y = 0;
        region.srcOffsets[1].x = (int32_t)((eye + 1) * c->srcW / 2);
        region.srcOffsets[1].y = (int32_t)c->srcH;
        region.srcOffsets[1].z = 1;
        int32_t dx0 = (int32_t)(eye * c->dstW / 2), dx1 = (int32_t)((eye + 1) * c->dstW / 2);
        region.dstSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        region.dstSubresource.layerCount = 1;
        region.dstOffsets[0].x = dx1;
        region.dstOffsets[0].y = (int32_t)c->dstH;
        region.dstOffsets[1].x = dx0;
        region.dstOffsets[1].y = 0;
        region.dstOffsets[1].z = 1;
        c->pCmdBlitImage(c->cmd, src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                         c->lin, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region, VK_FILTER_LINEAR);
    }

    VkImageMemoryBarrier b3 = b2;
    b3.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    b3.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    b3.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    b3.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    c->pCmdPipelineBarrier(c->cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                           0, 0, NULL, 0, NULL, 1, &b3);

    VkBufferImageCopy bic = {0};
    bic.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    bic.imageSubresource.layerCount = 1;
    bic.imageExtent.width = c->dstW;
    bic.imageExtent.height = c->dstH;
    bic.imageExtent.depth = 1;
    c->pCmdCopyImageToBuffer(c->cmd, c->lin, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, c->buf, 1, &bic);

    if (c->pEndCommandBuffer(c->cmd) != VK_SUCCESS) return;

    VkSubmitInfo si = {0};
    si.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    si.commandBufferCount = 1;
    si.pCommandBuffers = &c->cmd;
    if (c->pQueueSubmit(c->queue, 1, &si, c->fence) != VK_SUCCESS) { logmsg("capture: submit failed"); return; }
    c->pending = 1;
}

VKAPI_ATTR VkResult VKAPI_CALL cap_CreateSwapchainKHR(VkDevice dev, const VkSwapchainCreateInfoKHR *ci,
                                                     const VkAllocationCallbacks *ac, VkSwapchainKHR *sc) {
    VkResult r = g_create_sc(dev, ci, ac, sc);
    if (r == VK_SUCCESS && g_sc_count < MAX_SC) {
        ScInfo *s = &g_scs[g_sc_count];
        memset(s, 0, sizeof(*s));
        s->sc = *sc; s->format = ci->imageFormat; s->extent = ci->imageExtent;
        if (s->extent.width < 1024 || s->extent.height < 512) {
            logmsg("swapchain: %ux%u skipped (too small)", s->extent.width, s->extent.height);
            return r;
        }
        uint32_t n = 0;
        if (g_get_sc_images(dev, *sc, &n, NULL) == VK_SUCCESS && n > 0 && n <= 8) {
            s->count = n;
            g_get_sc_images(dev, *sc, &n, s->images);
        }
        g_sc_count++;
        logmsg("swapchain: %ux%u fmt=%d images=%u", s->extent.width, s->extent.height, (int)s->format, s->count);
    }
    return r;
}

VKAPI_ATTR void VKAPI_CALL cap_DestroySwapchainKHR(VkDevice dev, VkSwapchainKHR sc, const VkAllocationCallbacks *ac) {
    for (int i = 0; i < g_sc_count; ++i)
        if (g_scs[i].sc == sc) { g_scs[i] = g_scs[g_sc_count - 1]; g_sc_count--; break; }
    g_destroy_sc(dev, sc, ac);
}

VKAPI_ATTR VkResult VKAPI_CALL cap_QueuePresentKHR(VkQueue queue, const VkPresentInfoKHR *info) {
    if (g_enabled == 1 && info && info->swapchainCount > 0) {
        cap_collect(&g_cap);
        double t = now_s();
        int fps = env_int("VRSTREAM_CAP_FPS", 60);
        if (fps < 1) fps = 60;
        if (t - g_last_frame_t >= 1.0 / fps) {
            g_last_frame_t = t;
            // a present may carry several swapchains (main + small overlays);
            // we take the first large one we have registered
            for (uint32_t k = 0; k < info->swapchainCount; ++k) {
                ScInfo *sc = NULL;
                for (int i = 0; i < g_sc_count; ++i)
                    if (g_scs[i].sc == info->pSwapchains[k]) { sc = &g_scs[i]; break; }
                if (sc) {
                    cap_grab(&g_cap, sc, info->pImageIndices ? info->pImageIndices[k] : 0);
                    break;
                }
            }
        }
    } else if (g_enabled == 1) {
        cap_collect(&g_cap);
    }
    return g_present(queue, info);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL cap_GetDeviceProcAddr(VkDevice dev, const char *name) {
    if (g_cap.dev == VK_NULL_HANDLE && dev) g_cap.dev = dev;
    if (!strcmp(name, "vkQueuePresentKHR")) {
        if (!g_present) g_present = (PFN_vkQueuePresentKHR)g_gdpa(dev, name);
        return (PFN_vkVoidFunction)cap_QueuePresentKHR;
    }
    if (!strcmp(name, "vkCreateSwapchainKHR")) {
        if (!g_create_sc) g_create_sc = (PFN_vkCreateSwapchainKHR)g_gdpa(dev, name);
        return (PFN_vkVoidFunction)cap_CreateSwapchainKHR;
    }
    if (!strcmp(name, "vkDestroySwapchainKHR")) {
        if (!g_destroy_sc) g_destroy_sc = (PFN_vkDestroySwapchainKHR)g_gdpa(dev, name);
        return (PFN_vkVoidFunction)cap_DestroySwapchainKHR;
    }
    if (!strcmp(name, "vkGetSwapchainImagesKHR")) {
        if (!g_get_sc_images) g_get_sc_images = (PFN_vkGetSwapchainImagesKHR)g_gdpa(dev, name);
        return (PFN_vkVoidFunction)g_get_sc_images;
    }
    return g_gdpa(dev, name);
}

VKAPI_ATTR VkResult VKAPI_CALL cap_CreateDevice(VkPhysicalDevice gpu, const VkDeviceCreateInfo *ci,
                                                const VkAllocationCallbacks *ac, VkDevice *dev) {
    VkLayerDeviceCreateInfo *chain = (VkLayerDeviceCreateInfo *)ci->pNext;
    PFN_vkCreateDevice next_create = g_create_device;
    while (chain) {
        if (chain->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO && chain->function == VK_LAYER_LINK_INFO) {
            VkLayerDeviceLink *link = chain->u.pLayerInfo;
            g_gdpa = link->pfnNextGetDeviceProcAddr;
            g_gipa = link->pfnNextGetInstanceProcAddr;
            if (g_instance != VK_NULL_HANDLE) {
                PFN_vkCreateDevice nc = (PFN_vkCreateDevice)g_gipa(g_instance, "vkCreateDevice");
                if (nc) next_create = nc;
            }
            break;
        }
        chain = (VkLayerDeviceCreateInfo *)chain->pNext;
    }
    if (!next_create) { logmsg("FATAL: missing vkCreateDevice"); return VK_ERROR_INITIALIZATION_FAILED; }

    g_cap.phys = gpu;
    if (g_instance != VK_NULL_HANDLE && g_gipa)
        g_cap.pGetPhysMemProps = (PFN_vkGetPhysicalDeviceMemoryProperties)g_gipa(g_instance, "vkGetPhysicalDeviceMemoryProperties");

    VkResult r = next_create(gpu, ci, ac, dev);
    if (r != VK_SUCCESS) { logmsg("createDevice failed: %d", (int)r); return r; }

    g_cap.dev = *dev;
    g_cap.gdpa = g_gdpa;
    g_cap.queueFamily = ci->pQueueCreateInfos ? ci->pQueueCreateInfos[0].queueFamilyIndex : 0;
    g_cap.max_frames = env_int("VRSTREAM_CAP_MAX", 0);
    PFN_vkGetDeviceQueue pGetQueue = (PFN_vkGetDeviceQueue)g_gdpa(*dev, "vkGetDeviceQueue");
    if (pGetQueue) pGetQueue(*dev, g_cap.queueFamily, 0, &g_cap.queue);

    #define LOAD(field, name) g_cap.field = (PFN_##name)g_gdpa(*dev, #name)
    LOAD(pCreateImage, vkCreateImage);
    LOAD(pGetImageMemoryRequirements, vkGetImageMemoryRequirements);
    LOAD(pAllocateMemory, vkAllocateMemory);
    LOAD(pBindImageMemory, vkBindImageMemory);
    LOAD(pCreateBuffer, vkCreateBuffer);
    LOAD(pGetBufferMemoryRequirements, vkGetBufferMemoryRequirements);
    LOAD(pBindBufferMemory, vkBindBufferMemory);
    LOAD(pCreateCommandPool, vkCreateCommandPool);
    LOAD(pAllocateCommandBuffers, vkAllocateCommandBuffers);
    LOAD(pBeginCommandBuffer, vkBeginCommandBuffer);
    LOAD(pEndCommandBuffer, vkEndCommandBuffer);
    LOAD(pCmdPipelineBarrier, vkCmdPipelineBarrier);
    LOAD(pCmdBlitImage, vkCmdBlitImage);
    LOAD(pCmdCopyImageToBuffer, vkCmdCopyImageToBuffer);
    LOAD(pQueueSubmit, vkQueueSubmit);
    LOAD(pCreateFence, vkCreateFence);
    LOAD(pGetFenceStatus, vkGetFenceStatus);
    LOAD(pWaitForFences, vkWaitForFences);
    LOAD(pResetFences, vkResetFences);
    LOAD(pMapMemory, vkMapMemory);
    #undef LOAD

    if (!g_cap.pCreateImage || !g_cap.pCmdBlitImage || !g_cap.pCmdCopyImageToBuffer || !g_cap.pCreateCommandPool) {
        logmsg("capture: required functions missing, disabled");
        g_enabled = 0;
    }
    logmsg("device ok, queue=%p, capture=%s", (void *)g_cap.queue, g_enabled == 0 ? "off" : "on");
    return r;
}

VKAPI_ATTR VkResult VKAPI_CALL cap_CreateInstance(const VkInstanceCreateInfo *ci,
                                                  const VkAllocationCallbacks *ac, VkInstance *inst) {
    VkLayerInstanceCreateInfo *chain = (VkLayerInstanceCreateInfo *)ci->pNext;
    PFN_vkCreateInstance next_create = NULL;
    while (chain) {
        if (chain->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO && chain->function == VK_LAYER_LINK_INFO) {
            VkLayerInstanceLink *link = chain->u.pLayerInfo;
            g_gipa = link->pfnNextGetInstanceProcAddr;
            next_create = (PFN_vkCreateInstance)g_gipa(NULL, "vkCreateInstance");
            break;
        }
        chain = (VkLayerInstanceCreateInfo *)chain->pNext;
    }
    if (!next_create) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = next_create(ci, ac, inst);
    if (r == VK_SUCCESS) g_instance = *inst;
    return r;
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL cap_GetInstanceProcAddr(VkInstance instance, const char *name) {
    if (g_enabled < 0) {
        const char *e = getenv("VRSTREAM_CAP");
        g_enabled = (e && e[0] == '1') ? 1 : 0;
        logmsg("=== vrcap loaded, capture=%s ===", g_enabled ? "ON" : "off");
    }
    if (!strcmp(name, "vkGetInstanceProcAddr")) return (PFN_vkVoidFunction)cap_GetInstanceProcAddr;
    if (!strcmp(name, "vkGetDeviceProcAddr")) return (PFN_vkVoidFunction)cap_GetDeviceProcAddr;
    if (!strcmp(name, "vkCreateInstance")) return (PFN_vkVoidFunction)cap_CreateInstance;
    if (!strcmp(name, "vkCreateDevice")) {
        if (!g_create_device && g_gipa) g_create_device = (PFN_vkCreateDevice)g_gipa(instance, "vkCreateDevice");
        return (PFN_vkVoidFunction)cap_CreateDevice;
    }
    if (!g_gipa) return NULL;
    return g_gipa(instance, name);
}

__attribute__((visibility("default"))) VKAPI_ATTR VkResult VKAPI_CALL
cap_NegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface *p) {
    if (!p || p->sType != LAYER_NEGOTIATE_INTERFACE_STRUCT) return VK_ERROR_INITIALIZATION_FAILED;
    if (p->loaderLayerInterfaceVersion > 2) p->loaderLayerInterfaceVersion = 2;
    p->pfnGetInstanceProcAddr = cap_GetInstanceProcAddr;
    p->pfnGetDeviceProcAddr = cap_GetDeviceProcAddr;
    p->pfnGetPhysicalDeviceProcAddr = NULL;
    return VK_SUCCESS;
}
