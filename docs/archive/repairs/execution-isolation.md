# 执行与部署边界

## 必须了解

当前默认 `JMETER_EXECUTION_MODE=local` 保留现有脚本和分布式能力，**只适用于可信 JMX**。本地子进程仍拥有应用账户的文件权限；进程跟踪、工具确认、路径校验及过滤环境变量不等于 OS 沙箱。

生产部署采用单个应用 worker。多 worker 下确认队列、MCP SSE、调度任务及内存缓存尚未外置，不能保证跨进程所有权。MCP 当前支持 2024-11-05 旧式 SSE，不支持 Streamable HTTP。

网络默认监听 127.0.0.1；自定义访问域名必须列入 `ALLOWED_HOSTS`，前端来源必须列入 `CORS_ORIGINS`，均不得使用 `*`。生产必须设置独立 JWT_SECRET 和管理员密码，并使用 HTTPS。模型凭证不存浏览器。

## 可选容器模式

设置以下环境变量并重启服务（本次没有替用户修改环境配置）：

```text
JMETER_EXECUTION_MODE=container
JMETER_CONTAINER_IMAGE=your-registry/jmeter@sha256:<64位镜像摘要>
JMETER_CONTAINER_USER=65534:65534
JMETER_CONTAINER_MEMORY_MB=2048
JMETER_CONTAINER_CPUS=2
```

镜像须由运维准备并在本机可用，PATH 中提供 `jmeter` 命令及所需插件/证书。执行使用 `--pull=never`，不会自动下载或在失败时回退到本地模式。Docker CLI 和 daemon 都必须可用。测试机的 CPU/内存限制应按负载目标配置，避免将施压机瓶颈误判为被测系统容量。

容器以非 root 用户运行，根文件系统只读，删除所有 Linux capabilities，启用 no-new-privileges，并限制进程数、CPU、内存和临时目录。绑定范围仅为当前资源目录（只读）、输入快照（只读）、JTL/日志两个输出文件和 HTML 输出目录；不绑定整个后端或报告目录，不传递后端 API Key/JWT/数据库环境变量。取消时先删除该执行拥有的容器；无法确认清理完成时保留所有权并阻止新压测。

配置资源目录须位于 TEST_PLANS_DIR。计划中的 CSV/脚本路径应相对资源目录；不会重写 JMX 中的 Windows 绝对路径。脚本仍可执行，但不能写只读输入目录。Linux 绑定输出须预先安排非 root UID/GID 的写权限，不能以 privileged、root 或挂载 docker.sock 的方式绕过。容器模式暂不自动配置分布式 RMI 回连端口，会明确拒绝 `-R`；远程引擎的隔离由其部署负责。

容器网络仍需要访问被测服务；当前没有自动出站网络白名单。若执行不可信计划，需另行使用受限网络/防火墙。HTML 报告由测试生成，不应把未经审查的报告当可信网页；独立报告域名及专用只读服务仍是部署待完成项。

## 验证结果与限制

本机 Docker daemon 不可用。本次仅对命令构造、只读/可写绑定、非 root 参数、环境变量过滤和取消失败状态进行了模拟回归；**未进行容器实际启动、权限逃逸测试、远程引擎或真实负载验收**。

Windows 本地模式直接启动 Java 以持有真实 PID，保留常用 HEAP/GC/JVM_ARGS 参数；自定义 setenv.bat、JAVA9_OPTS 或插件启动定制需在环境中显式提供并在真实环境验证。不会执行任意 shell 启动脚本来猜测这些设置。

参考：[Docker 运行权限与限制](https://docs.docker.com/reference/cli/docker/container/run)、[非 root 运行](https://docs.docker.com/engine/security/rootless/)、[JMeter 启动参数](https://jmeter.apache.org/usermanual/get-started.html)。
