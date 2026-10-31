"""How to read a run. Reports, the analysis agent and MCP share these rules, so they reach the same conclusions."""

ANALYSIS_RULES = """## 解读压测结果的规则
- 所有数字必须来自提供的事实和证据，不要估算。分位数为 null 表示样本太少，不要自己推算。
- 判断负载是否符合预期只看 planned_load：线程数随时间升降是阶梯、尖峰测试的设计，不是执行异常。只有实际线程和 planned_load 不同才能这样说。
- JMeter 的 latency 包含建连。wait 是建连之后到首字节的等待，等于网络往返加服务端处理，客户端分不清两者。
- download 很小只说明响应体传得快，不能推出服务端不是主要耗时。要判断服务端耗时必须有服务端指标。
- steady 是并发达到峰值、且连接已复用的样本，不含爬坡、收尾和新建连接。
- 慢样本的 connect 高说明慢在建连。慢样本集中在某一时刻时要指出这一时刻，不要用爬坡或预热解释其它时段。
- 错误集中在一个接口时，先判断它是不是有意的反向用例。
- 解释原因时用【假设】标注，并写明能证伪它的数据或实验。"""
