/* tiling_probe — does the capture path give the same pixels on OPTIMAL tiling as on LINEAR?

   The capture layer blits the compositor's image into an intermediate and copies that into a host
   buffer. The intermediate used to be LINEAR only because nobody asked; OPTIMAL lets the GPU keep it in
   its own layout. This probe reproduces exactly that path offscreen — upload a known pattern, blit it
   downsampled, copy to a buffer — and compares the two tilings byte for byte. If they differ, the swap
   would put garbage on the wire and must not be made.

   Build on the headset:  gcc -O2 -o /tmp/tiling_probe tiling_probe.c -lvulkan
   Run:                   /tmp/tiling_probe
*/

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vulkan/vulkan.h>

#define SRC_W 64u
#define SRC_H 64u
#define DST_W 32u
#define DST_H 32u
#define CHECK(x) do { VkResult r_ = (x); if (r_ != VK_SUCCESS) { \
    fprintf(stderr, "%s failed: %d\n", #x, (int)r_); exit(1); } } while (0)

typedef struct {
    VkInstance inst; VkPhysicalDevice phys; VkDevice dev;
    VkQueue q; uint32_t family; VkCommandPool pool; VkCommandBuffer cmd;
} Ctx;

static uint32_t mem_type(Ctx *c, uint32_t bits, VkMemoryPropertyFlags want) {
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(c->phys, &mp);
    for (uint32_t i = 0; i < mp.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want) return i;
    fprintf(stderr, "no memory type for flags %u\n", (unsigned)want);
    exit(1);
}

static void init(Ctx *c) {
    VkApplicationInfo ai = {VK_STRUCTURE_TYPE_APPLICATION_INFO};
    ai.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo ici = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ici.pApplicationInfo = &ai;
    CHECK(vkCreateInstance(&ici, NULL, &c->inst));

    uint32_t n = 1;
    CHECK(vkEnumeratePhysicalDevices(c->inst, &n, &c->phys));
    if (n < 1) { fprintf(stderr, "no Vulkan device\n"); exit(1); }
    VkPhysicalDeviceProperties props;
    vkGetPhysicalDeviceProperties(c->phys, &props);
    printf("device: %s\n", props.deviceName);

    uint32_t qn = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(c->phys, &qn, NULL);
    VkQueueFamilyProperties *qf = calloc(qn, sizeof(*qf));
    vkGetPhysicalDeviceQueueFamilyProperties(c->phys, &qn, qf);
    int found = 0;
    for (uint32_t i = 0; i < qn; ++i)
        if ((qf[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) && (qf[i].queueFlags & VK_QUEUE_TRANSFER_BIT)) {
            c->family = i; found = 1; break;
        }
    if (!found) { for (uint32_t i = 0; i < qn; ++i) if (qf[i].queueFlags & VK_QUEUE_TRANSFER_BIT) { c->family = i; found = 1; break; } }
    free(qf);
    if (!found) { fprintf(stderr, "no transfer queue\n"); exit(1); }

    float prio = 1.0f;
    VkDeviceQueueCreateInfo qci = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    qci.queueFamilyIndex = c->family; qci.queueCount = 1; qci.pQueuePriorities = &prio;
    VkDeviceCreateInfo dci = {VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    dci.queueCreateInfoCount = 1; dci.pQueueCreateInfos = &qci;
    CHECK(vkCreateDevice(c->phys, &dci, NULL, &c->dev));
    vkGetDeviceQueue(c->dev, c->family, 0, &c->q);

    VkCommandPoolCreateInfo pci = {VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    pci.queueFamilyIndex = c->family;
    CHECK(vkCreateCommandPool(c->dev, &pci, NULL, &c->pool));
    VkCommandBufferAllocateInfo cai = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cai.commandPool = c->pool; cai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cai.commandBufferCount = 1;
    CHECK(vkAllocateCommandBuffers(c->dev, &cai, &c->cmd));
}

static VkImage make_image(Ctx *c, uint32_t w, uint32_t h, VkImageTiling tiling, VkDeviceMemory *mem) {
    VkImageCreateInfo ici = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ici.imageType = VK_IMAGE_TYPE_2D; ici.format = VK_FORMAT_B8G8R8A8_UNORM;
    ici.extent.width = w; ici.extent.height = h; ici.extent.depth = 1;
    ici.mipLevels = 1; ici.arrayLayers = 1; ici.samples = VK_SAMPLE_COUNT_1_BIT;
    ici.tiling = tiling; ici.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    ici.usage = VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    VkImage img;
    CHECK(vkCreateImage(c->dev, &ici, NULL, &img));
    VkMemoryRequirements mr;
    vkGetImageMemoryRequirements(c->dev, img, &mr);
    VkMemoryAllocateInfo mai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    mai.allocationSize = mr.size;
    mai.memoryTypeIndex = mem_type(c, mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    CHECK(vkAllocateMemory(c->dev, &mai, NULL, mem));
    CHECK(vkBindImageMemory(c->dev, img, *mem, 0));
    return img;
}

static void barrier(VkCommandBuffer cmd, VkImage img, VkImageLayout from, VkImageLayout to,
                    VkAccessFlags src, VkAccessFlags dst) {
    VkImageMemoryBarrier b = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.oldLayout = from; b.newLayout = to;
    b.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED; b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = img; b.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    b.subresourceRange.levelCount = 1; b.subresourceRange.layerCount = 1;
    b.srcAccessMask = src; b.dstAccessMask = dst;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
                         0, 0, NULL, 0, NULL, 1, &b);
}

/* Runs the layer's capture path once with the given tiling and hashes what lands in the buffer. */
static void run(Ctx *c, VkImageTiling tiling, int flip, unsigned char *out, size_t out_bytes) {
    VkDeviceMemory srcMem, midMem, bufMem;
    VkImage src = make_image(c, SRC_W, SRC_H, VK_IMAGE_TILING_LINEAR, &srcMem);
    VkImage mid = make_image(c, DST_W, DST_H, tiling, &midMem);

    VkBufferCreateInfo bci = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bci.size = out_bytes; bci.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    VkBuffer buf;
    CHECK(vkCreateBuffer(c->dev, &bci, NULL, &buf));
    VkMemoryRequirements bmr; vkGetBufferMemoryRequirements(c->dev, buf, &bmr);
    VkMemoryAllocateInfo bmai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    bmai.allocationSize = bmr.size;
    bmai.memoryTypeIndex = mem_type(c, bmr.memoryTypeBits,
                                    VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    CHECK(vkAllocateMemory(c->dev, &bmai, NULL, &bufMem));
    CHECK(vkBindBufferMemory(c->dev, buf, bufMem, 0));

    /* staging buffer with a pattern that changes every pixel and every channel */
    VkBufferCreateInfo sci = bci; sci.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    VkBuffer stage; CHECK(vkCreateBuffer(c->dev, &sci, NULL, &stage));
    VkMemoryRequirements smr; vkGetBufferMemoryRequirements(c->dev, stage, &smr);
    VkMemoryAllocateInfo smai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    smai.allocationSize = smr.size;
    smai.memoryTypeIndex = mem_type(c, smr.memoryTypeBits,
                                    VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VkDeviceMemory stageMem; CHECK(vkAllocateMemory(c->dev, &smai, NULL, &stageMem));
    CHECK(vkBindBufferMemory(c->dev, stage, stageMem, 0));
    void *p;
    CHECK(vkMapMemory(c->dev, stageMem, 0, VK_WHOLE_SIZE, 0, &p));
    unsigned char *px = p;
    for (uint32_t y = 0; y < SRC_H; ++y)
        for (uint32_t x = 0; x < SRC_W; ++x) {
            unsigned char *q = px + ((size_t)y * SRC_W + x) * 4;
            q[0] = (unsigned char)(x * 4); q[1] = (unsigned char)(y * 4);
            q[2] = (unsigned char)((x ^ y) * 3); q[3] = 255;
        }
    vkUnmapMemory(c->dev, stageMem);

    VkCommandBufferBeginInfo bi = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    CHECK(vkBeginCommandBuffer(c->cmd, &bi));

    barrier(c->cmd, src, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0,
            VK_ACCESS_TRANSFER_WRITE_BIT);
    VkBufferImageCopy up = {0};
    up.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    up.imageSubresource.layerCount = 1;
    up.imageExtent.width = SRC_W; up.imageExtent.height = SRC_H; up.imageExtent.depth = 1;
    vkCmdCopyBufferToImage(c->cmd, stage, src, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &up);
    barrier(c->cmd, src, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
    barrier(c->cmd, mid, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0,
            VK_ACCESS_TRANSFER_WRITE_BIT);

    /* the same swapchain-style flipped blit the layer does, NEAREST so the result is deterministic */
    VkImageBlit region = {0};
    region.srcSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    region.srcSubresource.layerCount = 1;
    region.srcOffsets[1].x = SRC_W; region.srcOffsets[1].y = SRC_H; region.srcOffsets[1].z = 1;
    region.dstSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    region.dstSubresource.layerCount = 1;
    if (flip) {
        region.dstOffsets[0].x = DST_W; region.dstOffsets[0].y = DST_H;   /* 180 degrees, as the layer does */
    }
    region.dstOffsets[1].z = 1;
    vkCmdBlitImage(c->cmd, src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, mid,
                   VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region, VK_FILTER_NEAREST);

    barrier(c->cmd, mid, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
    VkBufferImageCopy down = {0};
    down.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    down.imageSubresource.layerCount = 1;
    down.imageExtent.width = DST_W; down.imageExtent.height = DST_H; down.imageExtent.depth = 1;
    vkCmdCopyImageToBuffer(c->cmd, mid, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, buf, 1, &down);
    CHECK(vkEndCommandBuffer(c->cmd));

    VkSubmitInfo si = {VK_STRUCTURE_TYPE_SUBMIT_INFO};
    si.commandBufferCount = 1; si.pCommandBuffers = &c->cmd;
    CHECK(vkQueueSubmit(c->q, 1, &si, VK_NULL_HANDLE));
    CHECK(vkQueueWaitIdle(c->q));

    void *bp;
    CHECK(vkMapMemory(c->dev, bufMem, 0, VK_WHOLE_SIZE, 0, &bp));
    memcpy(out, bp, out_bytes);
    vkUnmapMemory(c->dev, bufMem);

    vkQueueWaitIdle(c->q);
    vkResetCommandBuffer(c->cmd, 0);
    vkDestroyBuffer(c->dev, stage, NULL); vkFreeMemory(c->dev, stageMem, NULL);
    vkDestroyBuffer(c->dev, buf, NULL); vkFreeMemory(c->dev, bufMem, NULL);
    vkDestroyImage(c->dev, mid, NULL); vkFreeMemory(c->dev, midMem, NULL);
    vkDestroyImage(c->dev, src, NULL); vkFreeMemory(c->dev, srcMem, NULL);
}

int main(void) {
    Ctx c = {0};
    init(&c);
    size_t bytes = (size_t)DST_W * DST_H * 4;
    unsigned char *lin = malloc(bytes), *opt = malloc(bytes);
    int flip = getenv("PROBE_FLIP") ? atoi(getenv("PROBE_FLIP")) : 1;
    printf("flipped blit: %s\n", flip ? "yes" : "no");
    run(&c, VK_IMAGE_TILING_LINEAR, flip, lin, bytes);
    run(&c, VK_IMAGE_TILING_OPTIMAL, flip, opt, bytes);

    size_t diff = 0, shown = 0;
    for (size_t i = 0; i < bytes; ++i)
        if (lin[i] != opt[i]) {
            ++diff;
            if (shown < 12) {
                size_t pix = i / 4;
                printf("  (%2zu,%2zu) ch%zu linear=%3u optimal=%3u\n", pix % DST_W, pix / DST_W,
                       i % 4, lin[i], opt[i]);
                ++shown;
            }
        }
    unsigned long long h1 = 1469598103934665603ULL, h2 = h1;
    for (size_t i = 0; i < bytes; ++i) { h1 = (h1 ^ lin[i]) * 1099511628211ULL; h2 = (h2 ^ opt[i]) * 1099511628211ULL; }
    printf("linear  fnv=%016llx first=%02x%02x%02x%02x\n", h1, lin[0], lin[1], lin[2], lin[3]);
    printf("optimal fnv=%016llx first=%02x%02x%02x%02x\n", h2, opt[0], opt[1], opt[2], opt[3]);
    printf("%s (%zu of %zu bytes differ)\n", diff ? "MISMATCH" : "identical", diff, bytes);
    return diff ? 1 : 0;
}
