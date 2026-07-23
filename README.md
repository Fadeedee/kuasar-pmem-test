# Kuasar Lazy PMEM Benchmark

本仓库归档 Kuasar Lazy PMEM 原型的测试方法、正式 benchmark harness、原始结果和
分析报告，用于独立审阅以下问题：

- 当前 `manifest:// + vhost-user-blk` 的行为；
- BLK 和 PMEM 共用 plaintext cache 后的公共收益；
- Lazy PMEM/DAX 在多 VM 高重合工作集下的额外页共享收益；
- file 与 memfd cache backing 的行为差异；
- cgroup 内存压力下的完成边界。

The repository contains the benchmark methodology, harness, raw evidence, and
derived reports for the Kuasar Lazy PMEM prototype. It is an evidence archive,
not a production deployment repository.

## 目录

```text
docs/
  benchmark_script_walkthrough.md  逐脚本、逐指标和逐校验项走读
  performance_report.md            正式性能结论
  rfc_draft.md                     架构提案草稿

harness/
  run_*.py                         采集器
  analyze_*.py                     分析器
  benchmark_*.py                   公共 workload/metric 模块
  test_*.py                        标准库单元测试
  dependencies/                    采集器复用的历史基础 runner

results/
  three-path/                      180 个主对比样本
  cache-backing/                   240 个 file/memfd 样本
  full-tree/                       120 个高重合工作集样本
  pmem-pressure/                   50 个 PMEM 压力 outcome
  blk-pressure/                    18 个 BLK 压力 outcome

tools/
  verify_archive.py                样本、schema、矩阵、哈希和复算审计
```

正式归档共包含 608 个 outcome：

```text
540 个正常功能/性能样本
 68 个内存压力 outcome
```

## 快速验证

只需要 Python 3.12 标准库：

```bash
make check
```

该命令会：

1. 对正式 Python 脚本执行 `py_compile`；
2. 运行 48 个标准库 `unittest`；
3. 使用每个 run 对应的 archived worker schema 校验 raw JSON；
4. 校验样本数量、唯一性、完整矩阵和脚本 SHA-256；
5. 校验所有成功压力样本的 worker result。

重新运行分析器并与归档 CSV/JSON/Markdown 做逐字节比较：

```bash
make reanalyze
```

这一步只读取 raw JSON，不启动 VM。

## 重要边界

- `plaintext-cold` 表示 lazyd plaintext cache 为空，不表示执行了 Host 全局
  `drop_caches`；
- 三路径结果应按 `Current BLK -> shared-cache BLK -> Lazy PMEM` 分层解释，
  不能把 shared cache 的全部收益归因于 PMEM；
- full-tree 是 165.95 MiB 高重合工作集诊断，不代表所有业务；
- 10 轮 p95 用于观察本次样本尾部，不等价于生产 SLA；
- pressure 中的 OOM 是被测 outcome，不是被删除的失败样本；
- 重新采样需要完整 Kuasar 产品 checkout、已构建二进制、root、systemd cgroup v2、
  KVM 和 TUN/TAP。仓库默认支持离线审计和复算，不提供一键部署环境。

完整方法和限制见
[`docs/benchmark_script_walkthrough.md`](docs/benchmark_script_walkthrough.md)。

## 主要结论

当前证据支持：

1. shared plaintext cache 是 BLK 和 PMEM 的公共收益来源；
2. Lazy PMEM 的独特价值是同 trust domain、同镜像 VM 复用最终不可变 EROFS
   Host 文件页；
3. 小工作集下 PMEM 的独立启动时延收益有限，但内存收益稳定；
4. 多 VM、高重合工作集下，PMEM 的扫描、CPU 和节点内存收益明显；
5. file 应继续作为默认 backing，memfd 作为不落明文路径的 volatile 选项。

详细数字见 [`docs/performance_report.md`](docs/performance_report.md)。

