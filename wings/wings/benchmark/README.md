# Wings 性能测试工具使用指南

## 概述

Wings 性能测试工具用于评估大语言模型推理服务的性能指标，包括吞吐量（TPS）、首 token 延迟（TTFT）等关键指标。

## 主要特性

1. **多引擎支持**：支持 VLLM、SGLang、Mindie、Wings 等推理引擎
2. **高性能异步架构**：
   - 基于 asyncio 和 aiohttp 的异步请求处理
   - Tokenizer 缓存机制，减少重复计算
   - HTTP 连接池复用，降低连接开销
   - 支持流式响应处理
3. **丰富的测试场景**：支持大语言模型、多模态大模型、支持不同并发度、输入输出长度、均匀分布等
4. **详细的性能报告**：CSV 和 JSON 格式输出
5. **批量测试**：支持多场景自动化测试和对比分析

## 快速开始

### 1. 数据准备

```bash
  # 对于大语言模型，可使用当前目录下的sonnet_20x.txt数据集，或者使用以下方法生成数据集。
  # 生成指定行数的 sonnet 文本数据集
  python data_generator.py --type text --output ./sonnet_100x.txt --text-lines 100
  
  # 对于多模态大模型的性能测试，可使用当前目录下的images中的数据集
  # 若需要其他格式的图片数据集，当前的生成逻辑是从互联网下载图片，需要联网环境。
  # 测试环境无网，应先从有网环境下载好图片，再传到测试环境。
  # 生成图片数据集
  python data_generator.py --type image --n 50 --height 512 --width 512
```

### 2. 基本使用

```bash
# 基本性能测试（使用异步架构）
python run_benchmark.py \
    --model-name model_name \
    --model-type llm \
    --model-path /path/to/model \
    --dataset-path /path/to/dataset \
    --ip 127.0.0.1 \
    --port 1025 \
    --thread-num 10 \
    --input-tokens-num 2048 \
    --output-tokens-num 2048

# 多模态大模型性能测试
python run_benchmark.py \
    --model-name model_name \
    --model-type mmum \
    --model-path /path/to/model \
    --dataset-path /path/to/dataset \
    --ip 127.0.0.1 \
    --port 1025 \
    --thread-num 10 \
    --input-tokens-num 2048 \
    --output-tokens-num 2048 \
    --image-height 512 \
    --image-width 512 \
    --image-count 10

# 测试maas平台的https
python run_benchmark.py \
    --model-path /path/to/model \
    --model-name model_name \
    --protocol https \
    --ip {ip}/serving-gateway/7102d45851a64c5a897c4b869280e025

# 在wings容器中的用法
  cd /opt
  python wings/benchmark/run_benchmark.py  \
	--model-name {model_name} \
    --model-path /weights \
    --ip {ip} \
    --port {port} \
    --thread-num 1 \
    --input-tokens-num 128 \
    --output-tokens-num 128
```


### 3. 批量测试

```bash
# 运行多个测试场景
python run_batch_test.py --config llm_batch_perf_test_config.json

# 只运行特定场景
python run_batch_test.py --config benchmark_config.json --scenario "高并发测试"

# 预览测试计划
python run_batch_test.py --config benchmark_config.json --dry-run

# 在wings容器中的用法，需要预先配置benchmark_config.json
cd /opt
python wings/benchmark/run_batch_test.py \
  --config benchmark_config.json
```

### 配置文件示例

```json
{
  "service": {
    "ip": "api.example.com",
    "port": "443",
    "protocol": "https",
    "ssl_verify": false
  },
  "model_name": "qwen",
  "model_path": "/path/to/model",
  "test_parameters": {
    "thread_num": 10,
    "input_tokens_num": 1024,
    "output_tokens_num": 512
  }
}
```

### 注意事项

1. **安全性**：
   - 生产环境中建议启用 SSL 验证
   - 测试环境可以跳过 SSL 验证以简化配置

2. **性能影响**：
   - HTTPS 会比 HTTP 有少量性能开销（通常 < 5%）
   - SSL 验证会带来额外的 CPU 开销

3. **端口设置**：
   - HTTPS 默认端口为 443
   - 确保服务器端口支持 HTTPS

### 常见问题

**Q: 什么时候需要跳过 SSL 验证？**
A: 在以下情况下可以跳过：
- 使用自签名证书
- 内部测试环境
- 证书配置不完整的开发环境

**Q: HTTPS 连接很慢怎么办？**
A: 可能的原因：
- SSL 握手开销（首次连接较慢）
- 网络延迟
- 服务器 SSL 配置问题

**Q: 如何验证 HTTPS 是否工作？**
A: 运行测试时会显示：
```
Service Address: https://api.example.com:443
SSL Verify: false
```

## 参数说明

### 基本参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--model-path` | 模型路径（必需） | - |
| `--model-name` | 模型服务名称 | qwen |
| `--ip` | 服务 IP 地址 | 127.0.0.1 |
| `--port` | 服务端口 | 18000 |
| `--protocol` | 协议类型 (http/https) | http |
| `--ssl-verify` | 启用 SSL 验证 | false |
| `--no-ssl-verify` | 禁用 SSL 验证（默认） | - |

### 测试参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-P, --thread-num` | 并发数 | 1 |
| `-I, --input-tokens-num` | 输入 Token 数 | 2048 |
| `-O, --output-tokens-num` | 输出 Token 数 | 2048 |
| `--uniform-interval` | 均匀请求间隔（秒） | 0 |
| `--warmup-num` | 预热请求数 | 0 |

### 性能选项

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--timeout` | 请求超时时间（秒） | 30 |
| `--warmup-num` | 预热请求数 | 0 |

## 配置文件格式

```json
{
  "test_mode": "async",
  "model_type": "llm",
  "model_name": "qwen",
  "model_path": "/path/to/model",
  "service": {
    "ip": "127.0.0.1",
    "port": "1025",
    "protocol": "http",
    "ssl_verify": false
  },
  "test_parameters": {
    "thread_num": 10,
    "input_tokens_num": 2048,
    "output_tokens_num": 2048,
    "uniform_interval": 0,
    "warmup_num": 5
  },
  "optimization": {
    "timeout": 30
  },
  "test_scenarios": [
    {
      "name": "低并发测试",
      "thread_num": 1,
      "input_tokens_num": 512,
      "output_tokens_num": 512
    }
  ]
}
```

## 性能指标说明

### 主要指标

1. **TTFT (Time to First Token)**：首 token 延迟（毫秒）
2. **TPS (Tokens Per Second)**：每秒处理的 token 数
   - TPS (with first token)：包含预填充阶段的 TPS
   - TPS (without first token)：仅解码阶段的 TPS
3. **TPOT (Time Per Output Token)**：每个输出 token 的平均时间（毫秒）
4. **Success Rate**：请求成功率（%）

### 计算方式

- TPS (with first token) = 总输出 token 数 / 总时间
- TPS (without first token) = (总输出 token 数 - 首个 token) / (总时间 - TTFT)
- TPOT = (总时间 - TTFT) / (总输出 token 数 - 1) * 1000

## 性能优化建议

### 1. 利用异步架构

```bash
# 使用异步架构进行高并发测试
python run_benchmark.py \
    --model-path /path/to/model \
    --thread-num 50
```

### 2. 调整并发度

- 低并发（1-10）：测试单请求性能和 TTFT
- 中并发（10-50）：测试服务稳定性
- 高并发（50+）：测试服务极限吞吐量

### 3. 合理设置超时时间

根据模型大小和请求长度设置合适的超时时间：
- 短文本（< 512 tokens）：10-30 秒
- 中等文本（512-2048 tokens）：30-60 秒
- 长文本（> 2048 tokens）：60-180 秒

### 4. 使用预热

建议在正式测试前进行 5-10 个预热请求，避免冷启动影响：
```bash
--warmup-num 10
```

### 5. 批量测试

使用批量测试脚本自动化运行多个测试场景：
```bash
# 运行预定义测试场景
python run_batch_test.py --config benchmark_config.json

# 查看测试计划
python run_batch_test.py --config benchmark_config.json --dry-run
```

## 故障排除

### 1. 常见错误

**连接错误**
```
Failed to establish connection to service
```
- 检查服务是否正常运行
- 验证 IP 和端口是否正确
- 对于 HTTPS，确认端口是否支持 HTTPS（通常为 443）

**SSL 证书错误**
```
SSL: CERTIFICATE_VERIFY_FAILED
```
- 使用 `--no-ssl-verify` 跳过证书验证（测试环境）
- 或使用 `--ssl-verify` 并确保服务器证书有效
- 检查证书是否过期或不受信任

**超时错误**
```
Request timeout
```
- 增加超时时间 `--timeout 60`
- 减少并发数 `--thread-num 5`

**内存不足**
```
Out of memory
```
- 减少并发数
- 减少输入输出长度

### 2. 数据集问题

如果使用自定义数据集，确保：
- 文件格式为 UTF-8 编码
- 每行一个样本
- 文件路径正确

### 3. 性能异常

如果测试结果异常：
1. 确保服务已充分预热（使用 `--warmup-num`）
2. 检查系统资源使用情况（CPU、内存、GPU）
3. 尝试降低并发度，逐步增加
4. 使用异步模式获得更准确的性能数据
5. 检查网络延迟和带宽

## 最佳实践

### 1. 测试前准备
- 确保服务稳定运行（建议至少运行 5 分钟）
- 准备多样化的测试数据集
- 了解服务配置和性能限制
- 检查系统资源（GPU、CPU、内存）使用情况

### 2. 测试执行策略
- **渐进式测试**：从低并发（1）开始，逐步增加到目标并发度
- **多维度测试**：不同输入长度、输出长度、并发度的组合
- **重复测试**：每个配置运行 3-5 次，取平均值
- **资源监控**：测试期间监控系统资源使用情况

### 3. 结果分析要点
- **性能指标**：TPS（吞吐量）和 TTFT（延迟）的平衡
- **稳定性**：错误率应低于 1%
- **资源效率**：GPU 利用率和内存使用情况
- **扩展性**：并发度增加时性能的变化趋势

### 4. 性能调优建议
- 根据实际业务场景选择合适的并发度
- 定期进行性能回归测试
- 建立性能基准线（baseline）
- 监控生产环境的实际性能指标

### 5. 高级技巧
- 使用批量测试脚本进行自动化测试
- 结合监控工具（如 Prometheus、Grafana）分析性能
- 对比不同模型版本或配置的性能差异
- 考虑网络延迟对性能的影响
- 利用异步架构的优势，合理设置并发度以获得最佳性能
