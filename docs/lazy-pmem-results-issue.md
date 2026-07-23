本文记录 Kuasar Lazy PMEM 的方案收敛和补充验证。原提案只与 Full PMEM
对比，不能说明相对现有 `manifest:// + vhost-user-blk` 的增量价值，因此重新
收敛了目标，并补充了 Current BLK、shared-cache BLK 和 Lazy PMEM 的直接对比。

## 1. 重新定义目标
当前方案拆成两层：

1. **shared plaintext cache**：BLK 和 PMEM 共用，复用已物化的明文范围，减少
   相同内容的重复解密和数据复制；
2. **Lazy PMEM/DAX**：可选 transport，使同一 trust domain、同一 content
   identity 的多个 VM 复用最终不可变 EROFS Host 文件页，减少每个 Guest 各自持有
   Rootfs page cache 的开销。

因此，PMEM 的核心目标是多 VM、高重合 Rootfs 工作集下的节点内存和 CPU。
普通 Manifest BLK 保持默认路径，Lazy PMEM 仅作为显式可选能力。

分层关系如下：

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
  canonical materialization + inflight dedup
  CacheBacking
    +-- file:  sparse regular file + persistent readiness map
    +-- memfd: sparse shmem object + volatile readiness map
        |
        v
FETCH + page-aligned ready range + read-only cache FD
        +------------------------------+
        |                              |
        v                              v
BLK + shared cache                 Lazy PMEM
cache FD pread/BlockReader         CH mmap(MAP_FIXED)
        |                              |
vhost-user-blk                     UFFD wake
        |                              |
Guest block I/O                    Guest EROFS DAX
```

上图描述的是启用 shared cache 后的两种可选消费路径。未配置 shared cache 时，
Current BLK 仍由 accelerator 的现有 Manifest ReaderAt 直接服务
vhost-user-blk，不经过 lazyd，默认行为保持不变。

accelerator 继续拥有 Kuasar 镜像格式语义；lazyd 只管理数据源无关的共享 cache；
Cloud Hypervisor 只看到通用的 `backend_id/backend_socket`，不理解 Manifest 或
lazyd 专有语义。这样可以单独使用 shared-cache BLK，也可以在目标场景选择 PMEM，
不会让普通 BLK 依赖 PMEM 路径。

### file/memfd 与 BLK/PMEM 的关系

`file/memfd` 是 lazyd 的 cache backing 策略，`BLK/PMEM` 是 Guest lower
transport，两个维度相互独立：

| | file backing | memfd backing |
|---|---|---|
| shared-cache BLK | cache FD `pread` -> vhost-user-blk | shmem FD `pread` -> vhost-user-blk |
| Lazy PMEM | cache FD `mmap` -> DAX | shmem FD `mmap` -> DAX |

两种 backing 使用相同的 content identity、canonical extents、物化窗口、inflight
去重和 FETCH wire protocol；BLK、Cloud Hypervisor 和 Guest 不需要知道 lazyd
选择了哪种 backing。

- `file` 是默认模式：创建私有 sparse EROFS 文件和持久 readiness map；写入并
  `sync_data` 后才标记 ready，clean file page 可以被内核回收并从本地文件重新
  fault-in。明文落盘场景要求受信、加密且有配额/TTL 的本地目录。
- `memfd` 模式：每个 content identity 创建一个 sparse shmem
  对象，不创建明文 cache 路径，readiness 只保存在 lazyd 内存中，lazyd 重启后需要
  重新物化。
- memfd 固定对象大小并设置
  `F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL`；不能设置 `F_SEAL_WRITE`，因为
  lazyd 仍需向缺失 range 写入数据。内部可写 file description 不会发送给消费者，
  BLK/PMEM 只能收到单独打开的 `O_RDONLY` FD。
- memfd 避免的是明文 filesystem pathname 和冷路径持久化写入，shmem 可能进入 swap，因此必须禁用或加密 swap。

对应测试中，8 VM Nginx cold 路径使用 memfd 后，shared-cache BLK/PMEM 的 Host
写入中位数分别从 `12.6/11.8 MiB` 降到 `1.5/1.5 MiB`；warm 路径没有稳定时延
优势。禁用 swap 的压力测试则显示 file backing 在极限内存附近多约
`16～32 MiB` 的可回收余量。因此当前选择是 file 保持默认，memfd 仅作为不希望
创建明文 cache 路径时的显式选项。

## 2. 三路径直接对比

我们使用同一 Nginx EROFS 镜像、同一 Guest HTTP 工作负载和同一 Host，测试
Current BLK、shared-cache BLK、Lazy PMEM，覆盖 cold/warm plaintext cache、
1/4/8 VM，每个 cell 10 个轮次，共 180 个样本。所有 Nginx 响应的长度和 SHA-256
一致。组级 Application Ready 是“最早一台 VM 开始启动”到“最晚一台 VM 可以
成功响应 HTTP”的时间。

下文的 `steady-state cgroup memory delta` 是 workload 完成、VM 仍运行时的
`memory.current` 减去 worker baseline。它包含该样本 cgroup 内的进程、Guest
内存以及归该 cgroup 计费的 anonymous、file page cache、shmem、页表和内核内存，
不是进程 RSS 求和；每个样本使用独立 cgroup 和 cache 对象，以降低 page-cache
首次计费归属的影响。该指标表示受测工作负载栈的内存增量，不等同于整台 Host 的
绝对物理内存占用。

8 VM 中位数如下：

| cache | 指标 | Current BLK | BLK + shared cache | Lazy PMEM |
|---|---|---:|---:|---:|
| cold | Application Ready | 2.801 s | 1.348 s | 1.112 s |
| cold | steady-state cgroup memory delta | 835.6 MiB | 628.1 MiB | 566.6 MiB |
| warm | Application Ready | 2.948 s | 1.038 s | 0.977 s |
| warm | steady-state cgroup memory delta | 836.6 MiB | 613.1 MiB | 565.7 MiB |

按 round 配对后：

- shared-cache BLK 相对 Current BLK，cold/warm Application Ready 分别改善
  56.3%/62.2%，steady-state cgroup memory delta 分别改善 24.8%/26.6%；
- Lazy PMEM 相对 shared-cache BLK，cold Application Ready 改善 18.2%
  （95% bootstrap CI 0.8%..25.4%），warm 改善 8.0%
  （CI -10.5%..12.0%，不显著）；
- Lazy PMEM 相对 shared-cache BLK，cold/warm steady-state cgroup memory
  delta 分别改善 9.9%/7.9%，两组均为 10/10 轮更低。

这组结果说明，小工作集的大部分启动收益来自公共 shared cache，不能归因于
PMEM；PMEM 在此场景中的稳定独立增量主要是内存。

## 3. 高重合工作集

为了放大并验证“最终文件页共享”这一独特能力，我们让每个 VM 遍历同一
openEuler EROFS 可见树并读取 165.95 MiB，覆盖 1/4/8 VM、BLK/PMEM、
file/memfd backing，每个 cell 10 轮，共 120 个样本。

8 VM 中位数：

| Transport | Backing | 全树扫描 | steady-state cgroup memory delta | operation CPU |
|---|---|---:|---:|---:|
| Shared BLK | file | 7.463 s | 2510.4 MiB | 70.02 s |
| Shared BLK | memfd | 6.012 s | 2511.6 MiB | 64.50 s |
| Lazy PMEM | file | 1.134 s | 963.6 MiB | 12.41 s |
| Lazy PMEM | memfd | 1.191 s | 955.7 MiB | 12.77 s |

![1/4/8 VM 高重合工作集扩展趋势](https://github.com/Fadeedee/kuasar-pmem-test/blob/main/results/full-tree/analysis/full_tree_backing.png?raw=1)

在相同 backing 下，PMEM 相对 shared-cache BLK：

- 全树扫描改善 76.7%～84.0%；
- steady-state cgroup memory delta 改善 61.6%～61.9%；
- operation CPU 改善 77.9%～81.9%；
- 两种 backing 都是 10/10 个配对轮次更优。

8 VM PMEM 映射的 RSS/PSS P50 为 `1392.2/174.0 MiB`，比例约为 8；所有 VM
使用同一个 cache identity，映射页为 Shared_Clean 且没有 Private_Dirty。这说明
多个 VMM 实际复用了同一组最终 EROFS Host 页。shared-cache BLK 可以复用取数和
解密结果，但 Guest page cache 仍然按 VM 建立，因此不能自然获得这一层共享。

这些收益只适用于同节点、同 trust domain、同镜像且工作集高度重合的场景。

## 4. Manifest、物化和映射粒度

当前设计把四种粒度明确解耦：

- Manifest chunk：accelerator 的存储、校验和解密单位；
- canonical extent：有序 Manifest chain 解析后的最终 Data/Hole/Zero 可见语义；
- materialization window：lazyd 在 Data extent 中切分的单个物化 extent 上限，
  默认 1 MiB；
- Guest fault/mapping：Guest fault 通常为 4 KiB，Cloud Hypervisor 映射 backend
  返回的、覆盖该 fault 的页对齐 ready range。

每个单独 materialization extent 不超过配置的 1 MiB 上限，不会沿整个 canonical
extent 扩张。一个边界页面可能与多个 Data extent 相交，因此当前实现可能为同一个
fault 发出多个、各自不超过 1 MiB 的 `read_range`；单个 fault 的聚合物化量尚未
形成严格的 1 MiB 协议上限。accelerator 为生成这些 range 需要读取和解密多少底层
chunk，仍由现有 Manifest 布局与 chunk cache 决定；API range 大小不等同于远端
实际下载量。

## 5. 共享边界和失败处理

当前原型已经实现并验证：

- cache identity 由有序 Manifest keys、最终 image size 和版本化 canonical
  规则生成，不包含 VM ID；
- lazyd 只返回匹配 backend identity 的只读 FD；
- Cloud Hypervisor 使用 `PROT_READ | MAP_SHARED | MAP_FIXED`，Guest lower
  只读，写入进入独立 upper；
- backend 超时、协议错误、FD/range 校验或 remap 失败会让 VM 明确失败，不能以
  零页掩盖数据错误或永久阻塞 vCPU；
- 首版明确不支持 snapshot/restore、迁移和 PROBE。

当前原型的部署约束是一个 lazyd/cache root/socket namespace 只服务一个 trust
domain；不互信 domain 必须使用独立实例和目录。file backing 必须位于受信、配额
受控且加密的本地存储；memfd 要求禁用或加密 swap。不可变内容发生变化时生成新的
content identity，不原地改写旧 cache。

产品化前仍需补齐显式的 trust-domain 授权绑定、consumer lease/refcount、cache
容量与 TTL/LRU 清理、每 backend 请求和物化限速，以及系统化故障注入。现有压力
数据只用于探索各自完成边界；由于 BLK 和 PMEM 使用的限额区间不同，不把它作为严格
横向性能结论。full-tree 用于验证高重合机制，后续还需要增加一个大工作集真实应用。

## 6. 总结

针对前述问题，补充了 Current BLK、shared-cache BLK 和 Lazy PMEM 的直接对比。
现有 BLK 已支持按需取数；公共明文 cache 复用已物化范围，减少重复解密和数据复制；
PMEM 的独特价值是让同镜像 VM 共享最终不可变的 EROFS Host 文件页。8 VM 高重合
工作集下，在相同 file/memfd backing 中，Lazy PMEM 相对 shared-cache BLK 将
steady-state cgroup memory delta 降低 61.6%～61.9%、CPU 降低
77.9%～81.9%、扫描时间降低 76.7%～84.0%。file 作为可持久化、可回收的默认
backing，memfd 作为避免明文文件路径和冷路径落盘写入的 volatile 选项；Lazy PMEM
则定位为同 trust domain、同镜像、多 VM 高重合场景的可选路径。

完整 raw 数据、采集/分析脚本和可复算报告将放在独立 benchmark 仓库：

- 仓库：<https://github.com/Fadeedee/kuasar-pmem-test>
- 性能报告：<https://github.com/Fadeedee/kuasar-pmem-test/blob/main/docs/performance_report.md>
- 测试方法：<https://github.com/Fadeedee/kuasar-pmem-test/blob/main/docs/benchmark_script_walkthrough.md>

当前归档包含 608 个 outcome；48 个脚本单元测试通过，所有 raw schema、样本矩阵
和脚本哈希校验通过，三个分析器从 raw 复算出的核心 JSON/CSV/Markdown 与归档
逐字节一致。
