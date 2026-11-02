# ProxyScope

轻量 **MITM 抓包代理 + 本地 Web 检索界面**。启动一个代理端口，让流量经过它，
所有请求与响应被记录下来，再通过浏览器界面按关键字 / 主机 / 状态码检索。

定位类似 Burp Suite / Yakit 的「Proxy + HTTP History」部分，但只做一件事：
**抓下来、存起来、查得到**。

---

## 快速开始

### Windows

```
start.bat                # 启动代理 (18888) + Web 界面 (18787)
install-ca.bat           # 首次使用：安装根证书（HTTPS 解密必需）
set-proxy.bat            # 让 Windows 系统代理指向 ProxyScope
```

用完恢复直连：`set-proxy.bat off`

### Linux / macOS

```bash
chmod +x start.sh        # 首次需要加执行权限
./start.sh               # 启动
```

脚本会自动检查 Python 版本、openssl、端口占用和缺失的可选依赖，并在启动前
打印全部访问地址。常用环境变量：

```bash
PROXY_PORT=18888 ./start.sh              # 换代理端口
LISTEN=0.0.0.0 ./start.sh                # 允许局域网连入（会给出安全提示）
WEB_PORT=18787 DATA_DIR=/var/lib/ps ./start.sh
VERIFY_UPSTREAM=1 ./start.sh             # 校验上游证书
./start.sh --help                        # 查看全部参数
```

然后浏览器打开 **http://127.0.0.1:18787** 查看抓到的流量。

> 只想给某个工具单独配代理、不动系统设置：
> `http://127.0.0.1:18888`（HTTP 与 HTTPS 共用同一个端口）。

---

## 让系统信任根证书

### Linux（Chrome / Chromium / curl / wget）

```bash
sudo cp data/certs/proxyscope-ca.crt /usr/local/share/ca-certificates/proxyscope.crt
sudo update-ca-certificates
```

Firefox 有独立证书库，需在界面里单独导入：
设置 → 隐私与安全 → 证书 → 查看证书 → 证书颁发机构 → 导入。

### macOS

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain data/certs/proxyscope-ca.crt
```

### 不装系统，只给单个工具用

```bash
curl --proxy http://127.0.0.1:18888 --cacert data/certs/proxyscope-ca.crt https://example.com/
export REQUESTS_CA_BUNDLE=$PWD/data/certs/proxyscope-ca.crt   # pip / requests
```

---

## 为什么需要装证书

HTTPS 要看到明文内容，代理必须在中间解密。ProxyScope 第一次运行会生成一套
自签 CA（`data/certs/proxyscope-ca.crt`），用它给每个访问的域名动态签发证书。

只有系统信任了这个 CA，浏览器才不会报证书错误。

卸载：Windows 用 `install-ca.bat remove`；Linux 删掉
`/usr/local/share/ca-certificates/proxyscope.crt` 后执行
`sudo update-ca-certificates --fresh`。

---

## 在 Linux 服务器上远程访问

代理跑在服务器、你想在本机浏览器操作时，**不要**把 Web 界面开在 0.0.0.0
（它没有登录认证，抓到的 Cookie / Token 会暴露给整个网络）。用 SSH 隧道：

```bash
# 服务器上：保持默认只监听回环
./start.sh

# 本机执行：把两个端口转发出来
ssh -L 18787:127.0.0.1:18787 -L 18888:127.0.0.1:18888 user@server
```

之后本机访问 `http://127.0.0.1:18787`，代理设为 `127.0.0.1:18888` 即可。

用 systemd 常驻（可选）：

```ini
# /etc/systemd/system/proxyscope.service
[Unit]
Description=ProxyScope
After=network.target

[Service]
User=youruser
WorkingDirectory=/opt/ProxyScope
ExecStart=/opt/ProxyScope/start.sh
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## 界面功能

| 区域 | 说明 |
|---|---|
| 左侧列表 | 流式列出所有请求：方法 / 状态码 / 时间 / 主机 / 路径 / 大小 / 耗时 |
| 搜索框 | 全文检索 URL、请求体、响应体（SQLite FTS5，命中正文内容） |
| 过滤条件 | host 模糊匹配、HTTP 方法、状态码（支持 `2xx` `4xx` `>=400`）、协议 |
| 右侧详情 | 概要 / 请求头 / 请求体 / 响应头 / 响应体 / 原始报文 六个标签页 |
| 自动刷新 | 每 2 秒刷新列表；检索模式下不会打断当前结果 |

细节：

- 响应体自动解压 gzip / deflate / br / zstd 后再入库和展示；
- JSON 自动格式化，图片直接内联预览；
- 大文件默认截断到 2 MB（详情页会标注「已截断」）；
- 记录默认最多保留 20 万条，超出自动清理最旧的。

---

## 命令行参数

```
python -m proxyscope [选项]

--listen 127.0.0.1     代理监听地址（默认仅本机；需要局域网共享用 0.0.0.0）
--port 18888           代理端口
--web-host 127.0.0.1   Web 界面地址
--web-port 18787       Web 界面端口
--data-dir ./data      数据目录（证书 + SQLite 数据库）
--verify-upstream      校验上游服务器证书（默认关闭，方便抓自签名站点）
--max-flows 200000     最多保留记录数
--timeout 60           上游超时（秒）
--debug                输出调试日志
--no-web               只跑代理，不启 Web 界面
```

---

## REST API

抓到的数据也能被脚本消费：

| 接口 | 说明 |
|---|---|
| `GET /api/flows` | 列表。参数：`q` `host` `method` `status` `scheme` `mime` `since` `until` `limit` `offset` `order` |
| `GET /api/flows/{id}` | 单条详情，含 base64 编码的请求/响应体 |
| `GET /api/stats` | 统计信息、代理运行状态、热点主机 |
| `POST /api/flows/delete` | `{"ids":[1,2]}` 删除指定记录 |
| `POST /api/flows/clear` | 清空全部记录 |
| `GET /api/ca.crt` | 下载 CA 证书 |
| `GET /api/health` | 健康检查 |

示例：

```bash
# 检索所有响应体里含 "password" 的 HTTPS 请求
curl "http://127.0.0.1:18787/api/flows?q=password&scheme=https"

# 只看 4xx/5xx
curl "http://127.0.0.1:18787/api/flows?status=4xx,5xx"
```

---

## 技术实现

- **纯标准库**：`asyncio` + `http.server` + `sqlite3`，无 Web 框架依赖；
- **MITM**：`CONNECT` 建隧道 → 用自签证书做 TLS 握手 → 解析明文 HTTP/1.1；
- **证书**：调用系统 OpenSSL 动态签发，内存缓存，不重复生成；
- **存储**：SQLite（WAL 模式），FTS5 全文索引，写入线程独立连接；
- 可选依赖 `brotli` / `zstandard`，缺失时对应编码不解码（其余功能不受影响）。

目录结构：

```
ProxyScope/
├─ proxyscope/
│  ├─ certs.py      CA 与证书签发
│  ├─ proxy.py      MITM 代理核心
│  ├─ store.py      SQLite 存储与检索
│  ├─ webui.py      Web 服务与 REST API
│  ├─ static/       前端界面
│  └─ __main__.py   命令行入口
├─ data/            运行时生成（证书、flows.db）
├─ start.bat
├─ install-ca.bat
└─ set-proxy.bat
```

---

## 已知限制

- 只处理 **HTTP/1.1**。HTTP/2 会降级为 1.1（`ALPN` 只协商 `http/1.1`）；
- WebSocket 升级后转为隧道转发，只记录握手，不记录帧内容；
- 不做 HTTPS 之外的协议（如 SOCKS、FTP）；
- 单进程 asyncio，适合个人抓包调试，不适合做大规模网关。

---

## 合规提醒

这个工具会记录经过代理的全部明文流量，包含 Cookie、Token、密码等敏感信息。

仅用于**你拥有或已获授权**的系统与流量。数据库文件 `data/flows.db` 是明文存储，
用完建议清空记录，或删除该文件。
