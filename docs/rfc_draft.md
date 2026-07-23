# RFC：共享 EROFS cache 与可选 Lazy virtio-pmem Rootfs lower

## 背景与问题定义

Kuasar 当前 `manifest:// + vhost-user-blk` 已能由 Guest 访问驱动按需读取相关
chunk。本 RFC 不重新实现 Manifest/chunk 协议，也不把“无需启动前完整下载”作为
PMEM 独有收益。

希望进一步验证和解决的是：同节点运行多个同镜像 VM 时，即使底层 chunk/store
已复用，最终明文 EROFS range 和 Guest page cache 仍可能按 VM 重复；当 Rootfs
工作集大且重合度高时，这会增加节点内存和 block transport 请求开销。

方案拆成两个层次：

1. **shared lazy cache**：BLK 和 PMEM 共用的内容寻址明文 EROFS sparse cache；
2. **Lazy PMEM/DAX**：可选 lower transport，让同 trust domain、同镜像 VM
   复用最终不可变 Host 文件页，减少逐 VM Guest page cache 副本。

默认 `manifest:// + vhost-user-blk` 不变。Lazy PMEM 是显式 opt-in，不替换现有
BLK，也不独占公共 cache 能力。

## 建议架构

```text
ordered Manifest keys
        |
        v
accelerator
  Manifest/chunk/store/校验/解密
  canonical visible ranges + plaintext range
        |
        v
lazyd
  content identity
  sparse plaintext EROFS cache
  readiness + inflight dedup
        |
        +------------------------------+
        |                              |
        v                              v
BLK + shared cache                 Lazy PMEM
BlockReader.ReadAt                 read-only cache FD
        |                              |
vhost-user-blk                     CH UFFD + MAP_FIXED
        |                              |
Guest block I/O                    Guest EROFS DAX
```

组件职责：

- **accelerator**：理解 Kuasar Manifest、chunk、store、校验和解密，返回最终
  可见 range；
- **lazyd**：管理内容级 cache identity、sparse EROFS cache、readiness、并发
  去重和 FETCH FD 协议，不重新解析 Kuasar 镜像格式；
- **sandboxer**：prepare cache、选择 lower transport、生成 VMM 参数和管理
  生命周期；
- **Cloud Hypervisor**：只理解通用 lazy backend，处理 pmem UFFD、FETCH、
  file-backed remap 和 wake，不感知 Manifest、chunk 或 cache 路径；
- **Guest**：BLK 路径保持现有块设备消费方式；PMEM 路径把只读 EROFS lower
  以 DAX 挂载，upper 仍使用现有可写块设备。

## 配置建议

`lower_device` 只选择 Guest transport；`lazy_cache` 独立控制是否启用公共 cache：

```yaml
boot:
  root:
    base: manifest://<ordered-manifest-keys>
    overlay:
      diff: file:///var/lib/kuasar/root.diff
    lower_device: vhost-user-blk # 或 lazy-pmem
    lazy_cache:
      lazyd_control_socket: /run/lazyd/lazyd.sock
      lazyd_data_socket: /run/lazyd/lazyd-data.sock
      accelerator_socket: /run/accelerator/manifest-range.sock
      materialization_max_bytes: 1048576
      alignment_bytes: 2097152
```

| lower transport | `lazy_cache` 未配置 | `lazy_cache` 已配置 |
|---|---|---|
| `vhost-user-blk` | 当前 Manifest reader，行为不变 | shared-cache BLK |
| `lazy-pmem` | 非法组合 | shared-cache PMEM/DAX |

Lazy PMEM 的 Cloud Hypervisor 参数保持通用：

```text
--pmem size=<aligned>,data_size=<image_size>,id=root-lower,
       discard_writes=on,lazy=on,backend_id=<content_id>,
       backend_socket=<data-socket>
```

不会把 sparse EROFS cache 路径作为普通 `file=` backend 传给 VMM，也不会在 VMM
参数中暴露 lazyd 专有命名。

lazyd 通过节点/trust-domain 级 `--cache-backing=file|memfd` 选择 backing，
sandboxer 不按 VM 传递该选项。它只决定 lazyd 如何承载最终明文 cache，不改变
BLK/PMEM 的 FETCH + SCM_RIGHTS 协议：

- `file`：默认模式，使用私有 sparse file 和持久 readiness map，支持重启恢复，
  clean page 可在内存压力下回收；
- `memfd`：实验性 volatile 模式，不创建明文 cache 路径和持久 bitmap，向消费者
  返回同一 shmem 对象的只读 FD；重启后需要重新物化。

设备布局保持：

```text
/dev/pmem0 -> sandbox-runtime.erofs
/dev/pmem1 -> lazy EROFS application Rootfs lower
/dev/vda   -> writable ext4 upper
```

## 数据身份和粒度

cache identity 由有序 Manifest key chain、最终 image size 和版本化 canonical
规则生成，不包含 VM ID。相同 identity 的 BLK/PMEM sandbox 复用同一 cache inode
和 readiness map。

四种粒度保持独立：

- **Manifest chunk**：accelerator 的存储、校验和解密单位；
- **canonical extent**：有序 Manifest chain 解析后的最终 Data/Hole/Zero 可见
  语义；
- **materialization window**：lazyd 在 Data extent 内切分的同步物化上限，默认
  1 MiB；
- **Guest fault/mapping**：Guest fault 通常为 4 KiB，CH 映射 lazyd 返回的覆盖
  fault 的页对齐 ready range。

一个 4 KiB fault 不会沿整个相邻 canonical extent 连锁物化；底层需要读取几个
chunk 仍由 accelerator 根据 Manifest 布局决定。

## PMEM 运行流程

1. sandboxer 根据 ordered Manifest keys 调用 lazyd prepare；
2. lazyd 调 accelerator describe，创建或复用 content-addressed sparse cache 和
   readiness map；
3. sandboxer 把 `blob_size/instance_id/lazyd_data_socket` 转换为 CH 的
   `data_size/backend_id/backend_socket`；
4. CH 创建 anonymous `MAP_NORESERVE` pmem HVA，注册 KVM mapping 和独立 UFFD
   missing range；
5. Guest 将 `/dev/pmem1` 以 EROFS `ro,dax=always` 挂为 OverlayFS lower；
6. 首次访问未映射页时 CH 向 backend 发送 FETCH；
7. lazyd 命中 ready range 时直接返回只读 cache FD；未命中时通过 accelerator
   物化受限窗口，持久化 cache 后再更新 readiness；
8. CH 校验 FD、identity、range 和文件大小，以 `MAP_SHARED | MAP_FIXED` 映射
   ready range并唤醒 vCPU；
9. 后续同 identity VM 复用同一 cache 和 Host page cache。

## 共享与安全边界

- sharing 继承 accelerator Manifest content-key/salt 域；租户级 ingest salt 会
  产生不同 Manifest keys 和 cache identity；
- 未使用 domain-separated keys 的部署，必须为不互信 trust domain 配置独立
  lazyd cache/socket namespace；
- lazyd 数据面 socket 是受文件权限约束的节点本地接口，只返回匹配 backend
  identity 的只读 FD；
- file backing 必须位于受信、配额受控且加密的本地存储；memfd 不暴露明文路径，
  但 shmem 可能进入 swap，因此必须禁用或加密 swap；
- memfd 和 file 都只允许同一 trust domain 共享；互不信任的 domain 使用独立
  lazyd socket/cache namespace；
- lower 只读，Guest 写入由独立 upper 承接；数据错误不能使用零页掩盖；
- 单 VM 请求速率、物化窗口和 cache 容量需要资源上限，防止扫描型 workload
  独占取数和 cache；
- 首版不支持 lazy root snapshot/restore 和迁移，相关请求明确返回 unsupported；
- backend 超时、协议错误、FD/range 校验或 remap 失败时，VM 明确失败，不能永久
  阻塞 vCPU。

## 原型和直接对比

原型已跑通：

```text
prepare -> Guest DAX fault -> CH FETCH -> lazyd/accelerator range
        -> read-only cache FD -> MAP_FIXED -> wake -> Guest continue
```

为回答“相比当前 BLK 的增量价值”，先使用同一 Nginx EROFS 镜像完成了 180 个
三路径正式样本：Current `manifest:// + vhost-user-blk`、shared-cache BLK 和
Lazy PMEM，覆盖 cold/warm、1/4/8 VM，每格 10 轮。Current BLK 保持现有
store/host cache 行为；后两条路径使用相同 lazyd content identity、canonical
extents、materialization window 和 file backing。

8 VM 中位数：

| cache | 指标 | Current BLK | BLK + shared cache | Lazy PMEM |
|---|---|---:|---:|---:|
| cold | Application Ready | 2.801 s | 1.348 s | 1.112 s |
| cold | steady-state cgroup memory delta | 835.6 MiB | 628.1 MiB | 566.6 MiB |
| warm | Application Ready | 2.948 s | 1.038 s | 0.977 s |
| warm | steady-state cgroup memory delta | 836.6 MiB | 613.1 MiB | 565.7 MiB |

按 round 配对：

- shared-cache BLK 相对 Current BLK，cold/warm Application Ready 改善
  56.3%/62.2%，steady-state cgroup memory delta 改善 24.8%/26.6%；
- Lazy PMEM 相对 Current BLK，cold/warm Application Ready 改善
  60.6%/65.2%，steady-state cgroup memory delta 改善 32.0%/32.5%；
- PMEM 相对 shared-cache BLK 的独立增量：cold Application Ready 改善
  18.2%（95% bootstrap CI 0.8%..25.4%），warm 改善 8.0%
  （CI -10.5%..12.0%，不显著）；cold/warm steady-state cgroup memory delta 改善
  9.9%/7.9%，每组均 10/10 轮更低。

这说明小工作集的大部分启动收益来自公共明文 cache，不能归因于 PMEM；PMEM
在该 workload 下提供稳定的额外内存收益，以及 cold 状态下的额外启动收益。

为进一步隔离 backing 选择和高重合工作集，另完成 428 个正式样本，让
shared-cache BLK/PMEM 使用同一个 content identity、canonical extents 和
materialization window，并分别测试 file/memfd：

- Nginx 小工作集共 240 个样本：cold/warm、1/4/8 VM、四种
  transport/backing 组合，每格 10 轮；小工作集主要证明协议兼容和复用路径，
  8 VM 启动时延存在调度抖动，不作为 PMEM 核心收益；
- openEuler 165.95 MiB 全树高重合工作集共 120 个样本，每格 10 个配对轮次；
  8 VM 下 PMEM 相对 shared-cache BLK 的配对中位改善如下：

| backing | 全树扫描 | post-launch cgroup memory delta | steady-state cgroup memory delta | operation CPU | PMEM 胜率 |
|---|---:|---:|---:|---:|---:|
| file | 84.0% | 67.5% | 61.6% | 81.9% | 10/10 |
| memfd | 76.7% | 67.6% | 61.9% | 77.9% | 10/10 |

- 8 VM PMEM 映射 RSS/PSS P50 为 1392.2/174.0 MiB，比例为 8.0；file 和
  memfd 均只有一个 cache identity，直接体现最终 EROFS 页在八个 VMM 间共享；
- 禁用 swap 的 cgroup 压力测试显示 file 具有更好的极限回收余量：
  PMEM 在 944 MiB 下 file 5/5 完成、memfd 0/5，memfd 到 976 MiB 后 5/5
  完成；BLK 在 2496 MiB 下 file 3/3 完成、memfd 0/3，memfd 到 2528 MiB 后
  3/3 完成；
- 因此 file 继续作为默认 backing；memfd 仅作为避免明文路径和冷路径落盘写入的
  volatile 选项，要求额外内存余量和禁用/加密 swap。

合计 608 个正式样本。数据支持的目标场景是同节点、同 trust domain、同镜像、
多 VM 且 Rootfs 工作集高度重合。小工作集中的 PMEM 增量约一成，不能外推为
所有应用或单 VM 都有大幅收益。

当前 BLK 全树读取的请求数明显高于 PMEM fault/mapping 数，CPU 和延迟仍可通过
BLK 请求合并、预取或调度优化。因此 RFC 把“最终不可变文件页共享和节点密度”作为
PMEM 的核心独特价值，不把全部延迟收益描述为 PMEM 不可替代。

## 建议的合入顺序

1. 先评审 shared cache identity、trust-domain 边界和 BLK 接入；
2. 再评审 Cloud Hypervisor 通用 lazy backend 与 opt-in PMEM transport；
3. 补 guest EROFS DAX 配置和 sandboxer PMEM 编排；
4. 并行优化 BLK range 合并/预取，继续作为直接对照；
5. 完成故障注入、长期内存压力和目标部署数据后，再决定是否扩大支持范围。

## 首版范围与验收

- 仅支持 `manifest://` overlay root 和一个 application EROFS lower；
- ordinary Manifest BLK 行为与 snapshot 路径不回归；
- BLK 和 PMEM 使用同一 content identity、cache inode 和 readiness map；
- cache hit 不访问 accelerator，cache miss 不超过 configured materialization window；
- PMEM fault/FETCH/mmap/wake 平衡，相邻未映射页仍继续 fault；
- 多 VM 映射同一只读 cache identity，无 private dirty；
- backend 异常时无永久 vCPU 卡死；
- file 为默认 cache backing；memfd 必须显式启用，且 sandbox 生命周期需要
  consumer lease，避免仍可能缺页时删除 volatile instance；
- snapshot/restore、迁移、PROBE 和动态策略不在首版范围。

希望先确认社区是否认可上述“公共 shared cache + 可选 PMEM/DAX transport”的分层
和目标场景，再按该顺序拆分 PR。
