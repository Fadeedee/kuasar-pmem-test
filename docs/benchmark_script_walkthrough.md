# Kuasar Lazy PMEM Benchmark 脚本详细走读

## 1. 文档目的

本文解释当前 Lazy PMEM 性能结论是怎样采集、校验和统计出来的，便于在评审时回答：

1. 测试到底启动了哪些真实组件；
2. 每个数字从哪里采集；
3. BLK、shared-cache BLK 和 Lazy PMEM 是否使用相同工作负载；
4. 如何确认 VM 读到了正确数据；
5. 如何确认 PMEM 确实走过 fault、FETCH、mmap 和 wake；
6. 如何排除漏样本、重复样本、执行顺序和 warmup 污染；
7. 当前数据能证明什么，不能证明什么。

本文只把生成当前正式结论的脚本作为主线。目录中更早的 startup、first-touch
和 reuse-scale 脚本会在附录中说明，但它们不参与当前 608 个正式样本的统计。

## 2. 一句话概括测试方法

每个样本都在独立的 systemd cgroup 中启动真实 accelerator/store、lazyd、
sandboxer、Cloud Hypervisor 和 Guest，运行同一项 Guest 工作负载，同时采集
整组进程的 cgroup 内存、CPU、I/O、进程 PSS、PMEM 映射和各组件协议计数，
最后先做完整性校验，再按同一轮次配对统计。

## 3. 当前正式数据集

当前文档中的 `608` 指 540 个正常功能/性能样本和 68 个内存压力结果：

| 数据集 | 路径 | 样本 |
|---|---|---:|
| 三路径 Nginx | `results/three-path` | 180 |
| file/memfd Nginx | `results/cache-backing` | 240 |
| openEuler 全树读取 | `results/full-tree` | 120 |
| Lazy PMEM 内存压力 | `results/pmem-pressure` | 50 |
| shared-cache BLK 内存压力 | `results/blk-pressure` | 18 |
| 合计 |  | 608 |

其中：

- 540 个正常功能样本全部生成完整 worker result；
- 68 个压力样本中 58 个完成工作负载，10 个被正确归类为 cgroup
  `memory-limit`；
- 压力测试中的 OOM 是被测结果，不是遗漏样本或脚本异常；
- 三路径的 180 个样本是当前最严格、最适合对外解释的主证据；
- full-tree 的 120 个样本用于验证高重合工作集下的页共享收益；
- pressure 的 68 个样本只用于比较 backing 在硬内存限制边缘的行为。

## 4. 三条被测路径

### 4.1 Current BLK

```text
Guest block read
    -> vhost-user-blk
    -> accelerator Manifest reader
    -> chunk/store
```

这是 Kuasar 当前 `manifest:// + vhost-user-blk` 路径，不启动 lazyd。

### 4.2 BLK + shared cache

```text
Guest block read
    -> vhost-user-blk
    -> lazyd shared plaintext EROFS cache
    -> accelerator range service on miss
```

这条路径把公共明文 cache 能力加入 BLK，用于拆分“shared cache 带来的收益”和
“PMEM/DAX transport 额外带来的收益”。

### 4.3 Lazy PMEM

```text
Guest EROFS DAX access
    -> Cloud Hypervisor UFFD fault
    -> lazyd FETCH
    -> accelerator range service on miss
    -> read-only cache FD
    -> MAP_SHARED | MAP_FIXED
    -> UFFD wake
```

这条路径与 shared-cache BLK 使用同一 lazyd cache 语义，但 Guest 通过
EROFS DAX 直接访问由多个 VMM 共享的 Host 文件页。

## 5. 脚本总调用关系

```text
run_three_path_evidence.py
run_cache_backing_evidence.py
run_full_tree_backing_evidence.py
run_cache_backing_pressure.py
        |
        | 每个 cell/round 启动独立 systemd service
        v
run_benchmark_worker.py
        |
        +-- benchmark_metrics.py
        |      cgroup memory/CPU/I/O 与组件 counter
        |
        +-- benchmark_workloads.py
        |      Nginx、full-tree、MySQL capability 工作负载契约
        |
        +-- run_reuse_benchmark.py
               VM 生命周期、日志 marker、PSS、PMEM smaps
                    |
                    v
            run_transport_benchmark.py
                    |
                    v
            run-performance.py
              store、range service、TAP、lazyd 基础启动

raw/*.json
        |
        +-- analyze_three_path_evidence.py
        +-- analyze_cache_backing.py
        +-- analyze_full_tree_backing.py
        +-- run_cache_backing_pressure.py 内置汇总
        |
        v
CSV + JSON audit + Markdown + SVG/PNG
```

## 6. 最重要的实验概念

### 6.1 sample

一个 sample 是一个确定的组合，例如：

```text
round=1
source_state=plaintext-cold
vm_count=8
mode=lazy-pmem
cache_backing=file
```

它对应一个独立 systemd transient service、一个 raw JSON 和一份 worker log。

### 6.2 cell

一个 cell 是去掉 round 后的固定组合。例如：

```text
plaintext-cold + 8 VM + lazy-pmem
```

正式三路径测试每个 cell 有 10 轮。

### 6.3 plaintext-cold

含义是该 sample 使用一个全新的 lazyd 明文 cache，不运行 warmup VM。

它不等价于“宿主机所有 page cache 和块设备 cache 都被清空”。脚本不会写
`drop_caches`，所以报告中必须称为 `cold plaintext cache`，不能称为绝对冷机。

### 6.4 plaintext-warm

同一个 sample 内先启动一台 warmup VM，等待工作负载完成，再停止 warmup VM。
之后记录 accelerator/lazyd counter baseline，才启动正式测量组。

因此正式阶段：

- 仍使用相同 backend 和相同 cache；
- warmup 的 FETCH 不计入正式阶段 counter；
- 分析器要求正式阶段 accelerator `read_range_bytes=0`；
- 分析器要求正式阶段 lazyd `materialized_bytes=0`。

### 6.5 并发 VM

脚本连续创建 N 个 VM，不等待前一个 VM ready 后再创建下一个，因此 VM 的启动和
工作负载阶段会重叠。

它不是使用一道严格同步 barrier 在同一个 CPU 指令时刻启动所有进程。
组级 Application Ready 定义为：

```text
最晚一台 VM 的 app_ready 时间 - 最早一台 VM 的启动时间
```

所以它表达的是“这一组 VM 全部可服务”的耗时。

### 6.6 Application Ready

Nginx workload 不是看到 VM boot 完成就算 ready。Guest 内会：

1. 启动真实 Nginx；
2. 循环请求 `http://127.0.0.1/`；
3. 只有 HTTP 请求成功后才输出 `KUASAR_BENCH_APP_READY`；
4. 随后再发起一次被测请求并计算返回内容 SHA-256。

因此 Application Ready 是应用可响应 HTTP 的时间，不是 VMM 进程创建时间。

## 7. `run_three_path_evidence.py`

路径：

`harness/run_three_path_evidence.py`

### 7.1 职责

它是三路径正式测试的顶层调度器，负责：

- 构造 `Current BLK / shared-cache BLK / Lazy PMEM` 矩阵；
- 构造 cold/warm 和 1/4/8 VM 矩阵；
- 每个 cell 执行 10 轮；
- 给每个样本创建独立 systemd cgroup；
- 记录二进制、脚本、Git revision 和 Host 环境；
- 支持安全续跑；
- 保存每个 worker 的日志；
- 遇到失败时记录精确 cell。

正式矩阵为：

```text
10 rounds
* 2 source states
* 3 VM counts
* 3 modes
= 180 samples
```

### 7.2 `mode_order`

脚本预定义三种模式的 6 种全排列，并根据 round 和 cell index 轮换。

目的不是随机化，而是让一种模式不会总在最前或最后运行，降低以下时间偏差：

- Host cache 逐渐变热；
- CPU 温度和频率变化；
- 后台任务干扰；
- 测试运行时间越靠后，系统状态越不同。

对应单元测试要求前 6 轮覆盖全部 6 种排列。

### 7.3 `build_run_contract`

开始测试前生成 `run-manifest.json`，记录：

- rounds、VM counts、source states 和 modes；
- workload 名；
- materialization window；
- cache backing；
- checkpoint 顺序；
- 所有被测二进制的大小和 SHA-256；
- 关键测试脚本 SHA-256；
- `images.tsv` SHA-256；
- accelerator、lazyd、Cloud Hypervisor、sandboxer 的 Git HEAD；
- 每个仓库 tracked diff SHA-256 和 `git status`；
- kernel、CPU 数、架构、KVM 和 cgroup v2 状态。

本次三个主要正式数据集记录的四个产品仓状态均为空，说明被测 worktree 没有未记录
的工作区修改。

如果输出目录已有 manifest，而新请求的 contract 不完全一致，脚本直接拒绝续跑。
这避免把不同代码、不同 VM 数或不同 workload 的样本混入同一结果目录。

### 7.4 `worker_command`

每个样本通过以下形式运行：

```text
systemd-run
  --collect
  --wait
  --pipe
  --unit=klp-e-<sample>
  MemoryAccounting=yes
  CPUAccounting=yes
  IOAccounting=yes
  python3 run_benchmark_worker.py ...
```

关键点是 sandboxer、VMM、lazyd 等后续子进程继承同一 cgroup，所以 cgroup 指标
不是只统计 Python worker。

### 7.5 `completed_sample`

续跑时不是看到 JSON 文件存在就跳过，而是：

1. 解析 JSON；
2. 调 `validate_worker_result` 校验 schema；
3. 校验 round、execution order、mode、state 和 VM count；
4. 全部匹配才标记 `SKIP`。

半写文件、旧 schema 或错误 cell 的 JSON 会被重新执行。

### 7.6 原子写入

manifest、status 和 worker result 都先写 `.tmp`，再用 `os.replace` 原子替换。

机器或脚本中断时，不会把半个 JSON 当成完整样本。

### 7.7 输出

```text
run-manifest.json
run-status.json
raw/rNN-{c|w}-{vm}-{b|s|p}.json
worker-logs/rNN-{c|w}-{vm}-{b|s|p}.log
```

本次状态：

```json
{"completed": 180, "skipped": 0, "failures": []}
```

## 8. `run_benchmark_worker.py`

路径：

`harness/run_benchmark_worker.py`

这是最关键的单样本执行器。

### 8.1 初始化

worker 首先校验：

- source state 只能是 cold/warm；
- backing 只能是 file/memfd；
- workload 必须是已知 workload；
- mode 必须是三条证据路径之一；
- 当前进程必须位于非根 cgroup v2 service。

如果 worker 仍位于 `/sys/fs/cgroup` 根节点，它会拒绝运行，因为此时无法隔离本样本
的资源数据。

### 8.2 五个采集点

采集顺序被固定为：

```text
worker_baseline
prelaunch
app_ready
operation_complete
held
```

含义如下：

| checkpoint | 含义 |
|---|---|
| `worker_baseline` | Python worker 启动后、backend/VM 启动前 |
| `prelaunch` | store/range/lazyd 已就绪，正式 VM 尚未启动 |
| `app_ready` | 组内每台 VM 都已输出应用 ready |
| `operation_complete` | 组内每台 VM 都完成被测操作 |
| `held` | operation complete 后等待 1 秒，VM 仍保持运行 |

`CgroupMemoryTracker` 和 `CgroupAccountingTracker` 强制按这个顺序采集。跳过、重复或
乱序都会报错。

`worker_baseline` 后会把 `memory.peak` 写为 0，重置该 sample 的 peak。

### 8.3 backend 启动差异

```text
Current BLK:
    不启动 lazyd
    Guest lower -> 当前 accelerator Manifest block path

shared-cache BLK:
    启动 lazyd
    Guest lower -> BLK -> lazyd shared cache

Lazy PMEM:
    启动 lazyd
    Guest lower -> PMEM/DAX -> CH -> lazyd shared cache
```

因此 current BLK 和另外两条路径之间的差值包含 shared cache 收益；
shared-cache BLK 和 Lazy PMEM 的差值才是 PMEM transport 增量。

### 8.4 warmup

warm sample 中：

1. 启动一台 index 0 warmup VM；
2. 等待 workload ready；
3. 保存 warmup 输出；
4. 停止 warmup VM；
5. 向 accelerator 和 lazyd 发送 `SIGUSR1`；
6. 等待它们输出结构化 counter snapshot；
7. 以此作为 measured baseline；
8. 再启动正式 VM 组。

如果组件退出、5 秒内不输出 counter 或 counter 字段变化，测试失败。

### 8.5 VM 组

worker 连续调用 `start_vm` 创建 1、4 或 8 台 VM，然后：

1. 等待所有 VM 的 `app_ready`；
2. 采集 `app_ready` cgroup；
3. 等待所有 VM 的最终 `ready`；
4. 采集 `operation_complete`；
5. 等待 1 秒；
6. 采集 `held`。

### 8.6 工作负载正确性

Nginx 每台 VM 返回：

```text
KUASAR_BENCH_RESULT_SHA256=<64 hex>
KUASAR_BENCH_BYTES=<positive integer>
```

worker 要求：

- 同组每台 VM 的 SHA-256 完全相同；
- 同组每台 VM 的响应字节数相同；
- warmup 与正式组结果相同；
- 返回字节数大于 0；
- marker 格式合法。

本次 180 个三路径样本全部得到：

```text
response bytes = 615
SHA-256 = fb47468a2cd3953c7131431991afcc6a2703f14640520102eea0a685a7e8d6de
```

所以“更快”不是通过跳过 Nginx 请求或读到不同页面得到的。

### 8.7 组级时间

`group_barriers` 使用：

```text
group_start = 所有 VM 中最早的 start_ns
某阶段完成 = 所有 VM 中最晚的该阶段 marker
```

记录：

- CH 全部启动；
- launch ack 全部完成；
- application 全部 ready；
- operation 全部开始；
- operation 全部完成；
- 组内最慢一次 operation 用时。

计时使用 `CLOCK_MONOTONIC_RAW`，不会被系统时间校准或 NTP 调整影响。

### 8.8 cgroup 指标

每个 checkpoint 读取：

```text
memory.current
memory.peak
memory.stat: anon/file/kernel/pagetables/slab/shmem
cpu.stat: usage/user/system
io.stat: rbytes/wbytes/rios/wios，跨设备求和
```

报告里的 `steady-state cgroup memory delta` 是：

```text
held_memory_current_bytes - worker_baseline_memory_current_bytes
```

这是整个 transient service cgroup 的内存增量，包含该样本中的 VM、VMM、
sandboxer、lazyd 以及被该 cgroup 计费的文件页。它不是只统计某一个进程的 RSS。

### 8.9 PSS 辅助指标

worker 遍历 sandbox 进程及其：

- `/proc/<pid>/task/<pid>/children` 后代；
- 相同 process group 成员；
- 每个进程的 `smaps_rollup`。

它汇总 sandbox 进程族 PSS，并单独加上 lazyd PSS。

PSS 是辅助指标。受测工作负载栈的内存比较优先使用 steady-state cgroup memory
delta，因为 PSS 不包含所有内核计费，也容易因进程边界遗漏。

### 8.10 PMEM 映射证据

Lazy PMEM 样本读取每个 CH 进程的 `/proc/<pid>/smaps`，找到 lazy cache mapping，
汇总：

- RSS；
- PSS；
- Shared_Clean；
- Private_Dirty；
- `(device, inode)` identity。

解释：

- RSS 会在每个映射进程中重复统计；
- PSS 会把同一物理页按共享者数量分摊；
- 8 VM 时 RSS/PSS 接近 8，说明这些 VMM 在映射同一组物理页；
- identity 只有一个，说明它们映射同一个 cache 对象；
- Private_Dirty 为 0，说明只读 lower 没有形成每 VM 私有脏副本。

### 8.11 组件 counter

accelerator：

```text
describe_requests
read_range_requests
read_range_bytes
read_range_max_bytes
```

lazyd：

```text
fetch_requests
fetch_request_bytes
fetch_returned_range_bytes
ready_hits
ready_misses
materialized_ranges
materialized_bytes
materialized_max_bytes
```

Cloud Hypervisor：

```text
data_faults
padding_faults
fetch_requests
fetch_request_bytes
mmap_ranges
mmap_bytes
wakes
```

vhost backend：

```text
read_requests
read_bytes
read_errors
loaded_blocks
total_blocks
```

counter 不是从模糊日志文本推测，而是组件响应 `SIGUSR1` 后输出 JSON marker。
warm sample 使用 final-baseline，只统计 warmup 后的正式组。

### 8.12 清理

无论成功还是异常，`finally` 都会：

- 反向停止所有 VM；
- 终止 lazyd/backend 进程组；
- 删除 socket 目录；
- 删除 sample runtime 目录。

这样可以降低上一轮残留进程或 socket 污染下一轮的概率。

## 9. `benchmark_metrics.py`

路径：

`harness/benchmark_metrics.py`

### 9.1 counter parser

`parse_counter_summaries` 只接受：

- 指定 marker；
- 一行内完整 JSON object；
- 所有 value 都是非负整数。

布尔值、字符串、负数或字段集合变化会被拒绝。

`subtract_counters` 要求 final 和 baseline 字段完全相同，并拒绝 counter 下降。

`sum_counters` 用于把 N 个 Cloud Hypervisor 的同名 counter 相加，字段不一致时报错。

### 9.2 cgroup 路径

`cgroup_path_from_proc` 只接受 `/proc/self/cgroup` 中唯一的 `0::...` unified cgroup v2
记录，并校验解析结果没有逃逸 `/sys/fs/cgroup`。

### 9.3 memory tracker

`CgroupMemoryTracker`：

- 固定五个 checkpoint；
- 禁止跳过；
- 禁止重复；
- 保存绝对值和相对 baseline delta；
- 允许 file cache 回收导致某个细分 delta 为负，不把它错误截断为 0。

### 9.4 accounting tracker

`CgroupAccountingTracker` 使用同样的 checkpoint 规则，记录 CPU 和跨设备 I/O。

## 10. `benchmark_workloads.py`

路径：

`harness/benchmark_workloads.py`

### 10.1 Nginx first request

正式三路径和 backing 数据使用它。

流程：

1. 启动 Nginx；
2. 最多尝试 300 次本地 HTTP 请求，每次间隔 100 ms；
3. Nginx 进程退出则立即失败；
4. 首次请求成功后输出 app-ready；
5. 等待 2 秒，使“应用启动”和“被测首次请求”分段；
6. 发起被测 HTTP 请求；
7. 输出响应 SHA-256 和字节数；
8. 输出 operation-end 和 ready；
9. 保持 VM 存活，供 steady-state cgroup memory delta 采样。

### 10.2 full-tree scan

正式 full-tree 和 pressure 数据使用它。

流程：

1. 输出 app-ready；
2. 输出 operation-begin；
3. 调 Guest 内 `/opt/sandbox-runtime/bin/read-tree`；
4. read-tree 遍历可见 EROFS 树并读取文件内容；
5. 输出总读取字节数；
6. 输出 operation-end 和 ready。

正式数据中每台 VM 读取：

```text
174,008,014 bytes = 165.95 MiB
```

这是一项高重合工作集诊断，不等价于通用业务应用启动时间。

### 10.3 MySQL capability smoke

它只检查 `mysqld`、`mysqladmin` 和 `mysql` 是否存在，没有启动数据库，也没有执行
SQL 查询。

因此它不能被描述成 MySQL Application Ready benchmark。当前 608 个正式样本也
没有用它得出性能结论。

### 10.4 输出 parser

`parse_workload_output` 严格校验：

- SHA-256 必须是 64 个小写十六进制字符；
- byte count 必须是非负十进制整数；
- capability 不能为空；
- 普通日志不会被误识别成 benchmark marker。

## 11. `run_reuse_benchmark.py`

路径：

`harness/run_reuse_benchmark.py`

它最初是复用实验脚本，当前 worker 复用了其中成熟的 VM 生命周期和观测能力。

### 11.1 workload device 对齐

raw device workload 中：

```text
Lazy PMEM             -> /dev/pmem1
Current/shared BLK    -> /dev/vda
```

除设备名外命令相同。正式 Nginx/full-tree 使用更高层的公共 workload contract。

### 11.2 `HeldVM`

`HeldVM`：

- 生成 sandbox 配置；
- 启动真实 `sandbox-ctl run`；
- 为 stdout/stderr 各启一个 reader thread；
- 每读到一行就立即用 monotonic raw clock 打时间戳；
- 保存完整日志；
- 提取 CH pid 和所有 marker；
- 检查 VM 是否提前退出；
- 等待 ready 超时后失败；
- 停止整个 process group。

reader 在日志行到达时打时间戳，不是等进程退出后再扫描日志，所以阶段时间可用于
时延统计。

### 11.3 `start_vm`

它为每台 VM 创建独立：

- runtime dir；
- log dir；
- short Unix socket dir；
- writable upper；
- TAP；
- sandbox YAML。

然后通过 transport contract 选择：

- `vhost-user-blk`；
- `vhost-user-blk-shared-cache`；
- `lazy-pmem`。

### 11.4 `selected_mapping_totals`

它逐 VM 读取 smaps，并用 device/inode 识别共享 cache，而不是只比较映射路径字符串。
memfd 没有普通磁盘路径时，会匹配 `memfd:lazyd-erofs-*`。

## 12. 基础运行器

### 12.1 `run_transport_benchmark.py`

路径：

`harness/dependencies/run_transport_benchmark.py`

当前正式 worker 动态导入它来复用：

- `Image` 和 `images.tsv` 解析；
- sandbox YAML 生成；
- `lazy_cache` 参数；
- lazy PMEM/shared BLK/current BLK transport 选择；
- Cloud Hypervisor 和 Guest 基础启动约定。

它把：

```text
materialization_max_bytes = 1 MiB
alignment_bytes = 2 MiB
```

写入 sandbox 配置。

### 12.2 `run-performance.py`

路径：

`harness/dependencies/run_performance.py`

`PhaseContext` 负责：

- 生成 store 和 Manifest range 配置；
- 启动真实 `store-ctl serve`；
- 启动真实 `manifest-ctl serve`；
- 创建每台 VM 的 TAP；
- 创建 256 MiB writable upper template；
- 等待 Unix socket ready；
- 退出时停止服务和删除 TAP。

`start_lazyd` 使用独立 mount namespace，把 sample cache bind mount 到 lazyd 的
`/var/lib/lazyd/images`，然后启动真实 lazyd control/data socket。

这意味着 cache 是每个 sample 独立的，而不是所有 cell 共用同一个临时目录。

## 13. `analyze_three_path_evidence.py`

路径：

`harness/analyze_three_path_evidence.py`

这是当前主证据的严格分析器。

### 13.1 完整矩阵

它从 `run-manifest.json` 读取 rounds、states、VM counts 和 modes，计算期望 key：

```text
(round, state, vm_count, mode)
```

以下任一情况都会失败：

- 少一个样本；
- 多一个样本；
- key 重复；
- 出现不在 contract 中的样本。

### 13.2 数据正确性

它要求 180 个样本：

- Nginx response SHA-256 完全相同；
- response bytes 完全相同；
- 每个样本有唯一 benchmark cgroup；
- vhost read error 为 0。

### 13.3 cold/warm 语义

cold shared-cache BLK 和 PMEM：

- materialized bytes 必须大于 0；
- lazyd materialized bytes 必须等于 accelerator read-range bytes；
- lazyd 最大 materialization 必须等于 accelerator 最大 range；
- 最大 range 必须不超过 1 MiB。

warm 正式阶段：

- lazyd materialized bytes 必须为 0；
- accelerator read-range bytes 必须为 0；
- 最大 range 必须为 0。

### 13.4 transport 闭环

Current BLK：

- 不允许出现 lazyd counter；
- 不允许出现 PMEM CH counter；
- 不允许调用 accelerator range service。

shared-cache BLK：

- lazyd FETCH 数必须等于 vhost root read request 数；
- 不允许出现 PMEM CH counter。

Lazy PMEM：

```text
data_faults == fetch_requests == mmap_ranges == wakes
CH fetch_requests == lazyd fetch_requests
CH fetch_request_bytes == lazyd fetch_request_bytes
CH mmap_bytes == lazyd fetch_returned_range_bytes
```

另外要求：

- 所有 VM 只有一个 cache identity；
- Private_Dirty 为 0；
- PSS 不大于 RSS；
- 多 VM 时 Shared_Clean 大于 0。

这组校验直接证明正式样本不是“VM 启动了，但未真正走 Lazy PMEM fault path”。

本次 audit 全部为 true：

```text
complete_paired_grid
workload_output_identical
vhost_read_errors_zero
warm_remote_fetch_bytes_zero
materialization_window_bounded
transport_working_sets_accounted_separately
pmem_fault_fetch_mmap_wake_balanced
pmem_single_cache_identity
pmem_private_dirty_zero
```

### 13.5 p50 和 p95

每个 cell 有 10 轮：

- p50 使用中位数；
- p95 使用排序后的线性插值；
- min/max 也保存在 CSV。

10 个样本的 p95 主要反映最慢两轮之间的尾部位置，不能等价成大规模生产分布的
“严格 95 分位 SLA”。

### 13.6 按 round 配对

比较不是简单做“两组独立中位数相除”，而是在相同：

```text
round + source_state + vm_count
```

内把两条路径配成一对，先计算每轮差值和改善百分比，再汇总。

这样可减少同一时间段系统抖动对不同路径比较的影响。

### 13.7 bootstrap CI

分析器对每组 paired delta/percentage 做 20,000 次有放回重采样，输出中位改善的
95% bootstrap CI。

随机种子由 cell、comparison 和 metric 的 SHA-256 稳定生成，所以重复分析会得到
完全一致结果。

CI 只表达“当前 10 个 paired rounds 的采样不确定性”，不表达跨硬件、跨内核或
跨 workload 的外推范围。

## 14. `run_cache_backing_evidence.py`

路径：

`results/cache-backing/source-snapshot/run_cache_backing_evidence.py`

矩阵：

```text
10 rounds
* 2 source states
* 3 VM counts
* 2 transports
* 2 backings
= 240 samples
```

被测组合：

```text
shared-cache BLK + file
shared-cache BLK + memfd
Lazy PMEM + file
Lazy PMEM + memfd
```

它与三路径 runner 使用相同：

- systemd cgroup；
- run contract；
- 原子输出；
- 安全 resume；
- execution order 轮换；
- Nginx workload。

区别是它不包含 Current BLK，目标是隔离 cache backing 的影响。

正式状态：

```json
{"completed": 240, "skipped": 0, "failures": []}
```

该数据集生成时使用的是较早一版 worker schema，`workload` 中尚无
`result_kind` 字段。其精确 worker 已保存在该 run 自己的
`source-snapshot/run_benchmark_worker.py`，SHA-256 与 run manifest 一致。
分析和复核时必须使用 run-specific snapshot，不能拿后来的 worker schema
直接校验旧 raw JSON。

## 15. `analyze_cache_backing.py`

路径：

`harness/analyze_cache_backing.py`

它输出：

- 每个 cell 的 mean/p50/p95/min/max；
- 同 round 下 memfd 相对 file 的 paired improvement；
- 同 round 下 Lazy PMEM 相对 shared-cache BLK 的 paired improvement；
- win rate；
- PMEM mapping RSS/PSS、identity 和 BLK read bytes。

主要指标：

```text
application_ready_seconds
first_operation_seconds
held_memory_bytes
held anon/file/shmem
launch memory growth
launch CPU
host writes
total PSS
```

### 15.1 当前边界

该分析器会检查：

- raw 非空；
- response hash 唯一；
- key 不重复；
- 每个 paired comparison 的两侧都存在。

但它没有像三路径分析器一样读取 manifest 并再次断言“每个 cell 必须正好 10 轮”。
本次 240 个样本仍可用，因为：

- runner 的 manifest 明确是 10 轮；
- `run-status.json` 是 completed=240、failures=[]；
- 原始 key 有 240 个且全部唯一；
- 重新分析得到与归档结果逐字节一致。

下一轮正式数据应把 manifest-based complete-grid 校验补到该分析器。

## 16. `run_full_tree_backing_evidence.py`

路径：

`harness/run_full_tree_backing_evidence.py`

矩阵：

```text
10 rounds
* 1 warm state
* 3 VM counts
* 2 transports
* 2 backings
= 120 samples
```

它与 backing runner 的主要区别：

- workload 改为 `full-tree-scan`；
- 每台 VM 读取同一个 165.95 MiB EROFS 可见树；
- 正式阶段只测 warm plaintext cache；
- 目标是放大多 VM 重合工作集和 Guest page cache 副本差异。

本次 run status：

```json
{"completed": 6, "skipped": 114, "failures": []}
```

这里的 `skipped=114` 是安全 resume，不是漏测。每个 skipped raw JSON 都通过
`validate_worker_result` 和 cell contract，且现有 run manifest 与请求 contract
完全一致。

## 17. `analyze_full_tree_backing.py`

路径：

`harness/analyze_full_tree_backing.py`

它比 backing 分析器多做以下严格检查：

- 根据 rounds、VM counts、mode 和 backing 构造完整矩阵；
- 拒绝缺样本、重复样本和额外样本；
- 每个样本必须是 full-tree workload；
- `bytes_per_vm` 数量必须等于 VM 数；
- 同组每台 VM 的 byte count 必须一致且大于 0；
- 所有样本的 tree byte count 必须一致；
- measured 阶段 accelerator/lazyd 不允许重新物化；
- PMEM 必须只有一个 cache identity；
- BLK 不允许伪造 PMEM mapping summary。

统计以相同 round 配对 file/memfd、BLK/PMEM，并给出中位改善和 win rate。

## 18. `run_cache_backing_pressure.py`

压力 run 自己保存了脚本：

```text
results/pmem-pressure/source-snapshot
results/blk-pressure/source-snapshot
```

### 18.1 与普通性能测试的区别

每个样本增加：

```text
MemoryMax=<limit>
MemorySwapMax=0
```

仍运行 8 VM warm-cache full-tree workload。

### 18.2 outcome 分类

```text
returncode=0 且存在完整 worker result -> pass
systemd/log 明确 OOM                 -> memory-limit
其他失败                             -> runtime-failure
```

它还从 `systemctl show` 保存：

- Result；
- ExecMainCode；
- ExecMainStatus；
- MemoryPeak；
- CPUUsageNSec。

### 18.3 为什么 OOM 样本有价值

压力测试的目标不是要求所有 cell 成功，而是寻找 backing 在硬限制下的完成边界。
因此 OOM 必须作为结构化结果保留，不能从统计中删除。

### 18.4 当前矩阵

PMEM：

```text
file/memfd
* 944/960/976/992/1024 MiB
* 5 rounds
= 50 outcomes
```

shared-cache BLK：

```text
file/memfd
* 2496/2528/2560 MiB
* 3 rounds
= 18 outcomes
```

两组内存限制不同，所以这组数据用于观察每种 transport 内 file/memfd 的边界，
不能直接拿 `944 MiB` 和 `2496 MiB` 当成严格同条件 transport benchmark。

## 19. 图表脚本

### 19.1 `render_cache_backing_svg.py`

只读取 `cell_summary.csv`，绘制 Nginx file/memfd 的 App Ready、Host writes 等曲线。

### 19.2 `render_full_tree_backing_svg.py`

只读取 full-tree 汇总 CSV，绘制扫描时间、内存和 CPU。

### 19.3 `render_backing_pressure_svg.py`

只读取 pressure summary，绘制不同 MemoryMax 下的 pass rate/边界。

图表脚本不重新定义样本、不修改 raw 数据，也不参与 paired comparison；
核心数字以 JSON/CSV 为准，PNG/SVG 只是展示层。

三路径图由 `analyze_three_path_evidence.py` 直接生成，数据同样来自已校验的
cell summary。

## 20. 单元测试文件逐项说明

### 20.1 `test_benchmark_metrics.py`

共 12 项，验证：

1. 结构化 counter marker 可解析；
2. warmup baseline subtraction 正确；
3. 多 CH counter 可求和；
4. 非整数和 counter 倒退被拒绝；
5. CPU stat 正确解析；
6. 多设备 I/O 正确求和；
7. unified cgroup v2 路径正确解析；
8. cgroup 路径逃逸被拒绝；
9. memory.stat 只保留稳定字段；
10. memory.current/peak/stat 可生成稳定列；
11. 内存回收导致负 delta 时不会被错误截断；
12. checkpoint 乱序、跳过和重复被拒绝。

### 20.2 `test_benchmark_workloads.py`

共 7 项，验证：

1. workload 名称固定；
2. full-tree marker 顺序；
3. Nginx 必须等 HTTP ready；
4. Nginx 被测响应会计算 SHA-256 和 byte count；
5. 所有命令通过 `/bin/sh -n` POSIX shell 语法检查；
6. MySQL smoke 不会被误称为 query benchmark；
7. marker parser 拒绝非法 SHA-256 和 byte count。

### 20.3 `test_benchmark_worker.py`

共 5 项，验证：

1. measured counter 会减掉 warmup baseline；
2. vhost 只统计目标 `blk0`，不混入另一块盘；
3. result schema 强制 checkpoint 顺序；
4. VM 行数必须等于 vm_count；
5. Nginx digest 和 full-tree byte counts 必须合法且一致。

### 20.4 `test_reuse_benchmark.py`

共 10 项，验证：

1. summary 包含 tail latency；
2. paired comparison 按 round 统计胜率；
3. raw workload 只因 transport 使用不同设备；
4. shared-cache BLK 走产品配置，不依赖隐藏 benchmark transport 开关；
5. direct workload 在正式读取前预热；
6. filesystem workload 调 read-tree；
7. named workload 使用统一 marker；
8. first-touch 和 repeat-touch 分段；
9. Host/Guest marker 能正确识别；
10. 缺失或倒序时间戳会失败。

### 20.5 `test_three_path_evidence.py`

共 3 项，验证：

1. 三模式覆盖全部 6 种执行顺序；
2. worker 命令一定启用独立 cgroup accounting；
3. resume 只接受 schema 和 contract 都匹配的样本。

### 20.6 `test_analyze_three_path_evidence.py`

共 9 项，验证：

1. quantile 线性插值；
2. 负收益不会被错误写成“降低”；
3. 缺一个 cell 时完整矩阵校验失败；
4. materialization 超过 1 MiB 时失败；
5. lazyd/accelerator 最大 range 不一致时失败；
6. BLK/PMEM 可以有不同累计工作集，不伪造相同访问模式；
7. paired comparison 按 round 对齐；
8. baseline 为 0 时不生成虚假百分比；
9. 单 VM preflight 图也能正确生成。

### 20.7 `test_cache_backing_evidence.py`

共 2 个标准库 `unittest`，验证：

1. 每个 backing round 都包含四种 transport/backing 组合；
2. worker command 会把 `--cache-backing=memfd` 正确传下去。

## 21. 本次脚本复核结果

### 21.1 Python 语法

14 个正式执行、采集、分析和渲染脚本均通过：

```text
python3 -m py_compile ...
```

### 21.2 单元测试

本次执行命令：

```bash
make test
```

标准库 `unittest` 自动发现 48 项：

```text
48 passed
0 failed
0 errors
```

独立证据仓只做了两项测试可移植性修正：

1. 把两个 pytest 风格裸函数改为标准 `unittest.TestCase`；
2. filesystem workload 测试直接验证 `read-tree` 位于 begin/end marker 之间，不再
   读取未归档的 `read-tree.go` 构建源。

这些修正没有改变采集器、分析器、raw 数据或归档报告。

### 21.3 raw schema 和矩阵

使用各 run manifest 对应的 archived worker schema 复核：

```text
three-path:   180 PASS
backing:      240 PASS
full-tree:    120 PASS
pressure:      68 个 outcome 唯一；所有 pass outcome 的 worker result PASS
```

### 21.4 分析可重复性

从 raw JSON 重新执行三个分析器后，以下核心文件与归档逐字节一致：

```text
three-path:
  audit.json
  analysis.json
  cell_summary.csv
  paired_comparisons.csv
  three_path_performance.md

cache-backing:
  analysis.json
  cell_summary.csv
  backing_pairs.csv
  backing_summary.csv
  transport_pairs.csv
  transport_summary.csv
  sharing_summary.csv
  cache_backing_performance.md

full-tree:
  cell_summary.csv
  backing_summary.csv
  transport_summary.csv
  sharing_summary.csv
  full_tree_backing_performance.md
```

这说明当前报告不是手工修改出来的，raw 数据经过同一脚本可以确定性地产生相同结果。

## 22. 如何复算

### 22.1 三路径

```bash
mkdir -p /tmp/kuasar-benchmark-audit/three
python3 \
  harness/analyze_three_path_evidence.py \
  --input results/three-path \
  --output /tmp/kuasar-benchmark-audit/three
```

### 22.2 backing

```bash
mkdir -p /tmp/kuasar-benchmark-audit/backing
python3 \
  harness/analyze_cache_backing.py \
  --raw-dir results/cache-backing/raw \
  --output-dir /tmp/kuasar-benchmark-audit/backing
```

### 22.3 full-tree

```bash
mkdir -p /tmp/kuasar-benchmark-audit/tree
python3 \
  harness/analyze_full_tree_backing.py \
  --raw-dir results/full-tree/raw \
  --output-dir /tmp/kuasar-benchmark-audit/tree \
  --rounds 10
```

复算不启动 VM，只读取 raw JSON。

重新采样则会启动真实 KVM VM、TAP、systemd service 和 backend，需要 root、
`/dev/kvm`、cgroup v2、TUN/TAP、足够内存和测试网络环境，不应在共享生产节点直接
执行。

## 23. 当前数据可以证明什么

### 23.1 可以证明

1. 当前三条路径都能完成相同 Nginx 请求并返回完全相同内容；
2. shared plaintext cache 能复用已物化明文范围，减少重复解密和数据复制；
3. 在同样使用 shared cache 时，Lazy PMEM 在 8 VM 下继续降低 steady-state
   cgroup memory delta；
4. 高重合 full-tree workload 下，PMEM 明显减少逐 VM Guest page cache 副本；
5. 多个 CH 映射的是同一个 cache identity，页面是 Shared_Clean，无 Private_Dirty；
6. PMEM 正式样本逐个满足 fault/FETCH/mmap/wake counter 闭环；
7. warm 正式阶段没有重新访问 accelerator 物化数据；
8. 单个同步 materialization extent 没有超过 1 MiB 配置窗口；
9. file backing 在硬内存限制边缘比 memfd 多约 16 至 32 MiB 回收余量。

### 23.2 不能证明

1. 不能证明 Lazy PMEM 在所有单 VM workload 都快于 BLK；
2. 不能把 shared cache 的全部收益归因于 PMEM；
3. 不能证明 10 轮 p95 等价于生产 SLA；
4. 不能把 full-tree 高重合扫描外推到所有业务；
5. 不能用 plaintext-cold 声称整个 Host page cache 绝对冷；
6. 不能用当前 pressure 数据直接比较 PMEM 944 MiB 和 BLK 2496 MiB 的严格同限额性能；
7. 不能替代裸机、多 NUMA、不同内核和真实集群的后续验证；
8. 不能证明 snapshot/restore、migration 和 backend 故障恢复已经可用于生产。

## 24. 当前审计发现的改进项

### 24.1 高优先级

后续正式复测时，把以下动态依赖复制并记录 SHA-256：

```text
run_transport_benchmark.py
run-performance.py
read-tree 的构建来源
```

本仓已经归档两个 Python 基础 runner；当前 manifest 已固定主 runner、worker、
产品仓和二进制，但被动态导入的辅助脚本尚未全部进入每个 run contract。

### 24.2 中优先级

让 `analyze_cache_backing.py`：

- 读取 `run-manifest.json`；
- 构造完整 expected grid；
- 强制每 cell 正好为 contract 中的 rounds；
- 输出独立 audit.json。

### 24.3 中优先级

让 pressure manifest 额外记录：

- pressure runner 自身 SHA-256；
- 四个产品仓 revision；
- Host/kernel/KVM/cgroup 环境。

### 24.4 低优先级

后续重新采样时归档 `read-tree` 的构建源和构建命令，增强二进制来源审计。

这些改进影响的是独立审计和长期复现强度，不表示当前 raw 数据或统计公式存在已知
错误。

## 25. 结果解读顺序

### 25.1 问题边界

```text
现有 BLK 已经按需读取 chunk，本方案不重复解决该问题。
实验先为 BLK 和 PMEM 提供同一 shared plaintext cache，再比较 PMEM 的独立增量。
```

### 25.2 实验公平性

```text
三条路径使用同一 Nginx 镜像、同一 Guest HTTP 工作负载、同一 Host、
同一轮次配对；每个样本位于独立 cgroup，并轮换执行顺序。
```

### 25.3 正确性

```text
180 次 Nginx 响应的字节数和 SHA-256 全部一致；
PMEM 每个样本的 fault、FETCH、mmap、wake 计数闭合；
warm 阶段远端取数为 0。
```

### 25.4 结果结论

```text
小工作集的大部分收益来自 shared cache；
PMEM 的独特收益是多个同镜像 VM 共享最终 EROFS Host 文件页，
因此在 VM 数量、工作集和重合度提高时收益更明显。
```

### 25.5 适用边界

```text
这是一组单节点原型证据，不宣称替代 BLK，也不外推到所有 workload。
当前建议是把 shared cache 作为公共能力，把 Lazy PMEM 作为高重合、多 VM 场景的
可选 transport。
```

## 26. 常见追问

### 为什么不用单进程 RSS？

单进程 RSS 会重复计算共享页，也会漏掉 lazyd、sandboxer 和内核计费。
主指标使用 steady-state cgroup memory delta，PSS 和 smaps 只用于解释共享机制。

### 为什么还要 PSS？

RSS/PSS 比例可以证明“多个 VMM 看到了同一批物理页”。8 VM 下 RSS/PSS 接近 8，
比只看总内存更直接地解释共享来源。

### 为什么要 current BLK、shared BLK、PMEM 三条？

只有两条时无法区分收益来自 shared cache 还是 PMEM。三条路径把公共 cache 收益和
PMEM transport 增量拆开。

### 为什么要 cold 和 warm？

cold 检查首次 materialization；warm 检查 cache hit 和纯 transport 行为。
两者回答的问题不同。

### 为什么不能只看平均值？

平均值容易被单次抖动拉动，所以报告同时保存 median、p95、min、max，并按 round
配对计算改善和 win rate。

### 为什么 full-tree 比 Nginx 差异大？

Nginx 首次请求只访问较小工作集；full-tree 让 8 台 VM 读取同一 165.95 MiB 内容，
会放大 BLK 每个 Guest 独立 page cache 与 PMEM 共享 Host 页之间的差异。

### 这些图能否单独作为证据？

不能。图只是 CSV 的展示层。评审时应同时给出：

```text
run-manifest.json
run-status.json
analysis/audit.json
cell_summary.csv
paired_comparisons.csv
raw sample
```

## 27. 历史探索脚本

以下脚本帮助过早期实验设计，但不参与当前 608 个正式样本：

| 脚本 | 作用 | 当前定位 |
|---|---|---|
| `run_concurrent_startup_benchmark.py` | 早期并发启动比较 | 探索性 |
| `run_staged_concurrent_startup_benchmark.py` | 拆分 CH、launch、app 阶段 | 探索性 |
| `run_first_touch_benchmark.py` | 区分首次和重复全树读取 | 探索性 |
| `run_reuse_scale_repeated.py` | 早期 1/2/4/8 VM 复用趋势 | 探索性 |
| `render_charts.py` | 早期图表 | 不用于当前报告 |
| `render_concurrent_startup_charts.py` | 早期并发图表 | 不用于当前报告 |
| `render_redesigned_benchmark_charts.py` | 早期重设计图表 | 不用于当前报告 |
| `render_reuse_repeated_charts.py` | 早期复用趋势图 | 不用于当前报告 |

这些脚本不能与当前主数据混用，因为早期矩阵、指标定义、并发方式和产品版本可能不同。

## 28. 评审时建议展示的文件

主报告：

- `docs/performance_report.md`
- `results/three-path/analysis/three_path_performance.md`

审计：

- `results/three-path/run-manifest.json`
- `results/three-path/run-status.json`
- `results/three-path/analysis/audit.json`

统计：

- `results/three-path/analysis/cell_summary.csv`
- `results/three-path/analysis/paired_comparisons.csv`

单样本示例：

- `results/three-path/raw/r01-c-8-p.json`

full-tree：

- `results/full-tree/analysis/full_tree_backing_performance.md`

pressure：

- `results/pmem-pressure/pressure_report.md`
- `results/blk-pressure/pressure_report.md`
