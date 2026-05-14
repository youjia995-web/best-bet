# 云端部署说明

这个项目会在后台定时采集数据，并用 SQLite 保存赔率、回测和状态文件。你已经购买阿里云轻量服务器的话，优先用服务器部署，这是最适合长期运行的方式。

## Zeabur 部署

仓库根目录已经包含 `Dockerfile`，Zeabur 会自动识别并用 Docker 部署。启动命令会读取 Zeabur 注入的 `PORT`，并运行：

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

### 1. 从 GitHub 创建服务

1. 打开 Zeabur Dashboard。
2. 创建 Project，选择你购买的 Server / Cloud Service。
3. 新建 Service，选择 GitHub Repo。
4. 选择仓库：`youjia995-web/best-bet`。
5. Root Directory 保持仓库根目录，不要改到子目录。

### 2. 配置环境变量

在 Zeabur 服务的 Environment Variables 里添加：

- `ADMIN_PASSWORD`：后台操作密码，建议改成强密码。
- `ODDS_API_IO_KEY`：备用数据源 key；如果只用主接口可先留空。
- `DATA_DIR=/data`
- `DEFAULT_DATA_SOURCE=odds_api_io`：可选，如果希望云端启动后默认使用备用数据源。

备用源额度保护相关变量可按需添加：

- `ODDS_API_IO_MIN_INTERVAL=180`
- `ODDS_API_IO_EVENT_LIMIT=300`
- `ODDS_API_IO_BOOKMAKERS=Bet365,Unibet`

### 3. 挂载持久化 Volume

这个项目默认使用 SQLite。为了避免 Zeabur 重启或重新部署后丢失数据库，请给服务添加 Volume，并挂载到：

```text
/data
```

项目会把这些运行文件写到 `/data`：

- `odds_monitor.db`
- `data_source_state.json`
- `odds_api_io_state.json`
- `odds_api_io.key`（如果你不用环境变量，也可以放这里）

### 4. 公开访问

部署成功后，在 Zeabur 的 Networking / Domain 页面生成公开域名或绑定自己的域名。访问 Zeabur 给出的 HTTPS 地址即可打开监控页面。

### 5. 更新部署

以后本地改完代码并推送到 GitHub `main` 分支，Zeabur 会按 GitHub 集成自动重新部署。

## 阿里云轻量服务器部署

以下命令按 Ubuntu/Debian 系统编写。CentOS/Alibaba Cloud Linux 也可以部署，但安装命令会略有不同。

### 1. 连接服务器

```bash
ssh root@你的服务器公网IP
```

### 2. 安装基础环境

```bash
apt update
apt install -y git python3 python3-venv python3-pip
```

### 3. 拉取项目

```bash
cd /opt
git clone https://github.com/youjia995-web/best-bet.git
cd /opt/best-bet
```

如果服务器上已经 clone 过，以后更新代码用：

```bash
cd /opt/best-bet
git pull
```

### 4. 安装 Python 依赖

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 5. 配置数据目录

```bash
mkdir -p /opt/best-bet-data
```

项目会把 SQLite 数据库和状态文件保存到这里：

- `/opt/best-bet-data/odds_monitor.db`
- `/opt/best-bet-data/data_source_state.json`
- `/opt/best-bet-data/odds_api_io_state.json`

### 6. 配置 systemd 开机自启动

复制服务模板：

```bash
cp /opt/best-bet/deploy/best-bet.service /etc/systemd/system/best-bet.service
```

编辑服务文件，把 `ADMIN_PASSWORD` 改成你的管理密码；如果有备用接口 key，也填写 `ODDS_API_IO_KEY`：

```bash
nano /etc/systemd/system/best-bet.service
```

启动服务：

```bash
systemctl daemon-reload
systemctl enable best-bet
systemctl start best-bet
systemctl status best-bet --no-pager
```

查看日志：

```bash
journalctl -u best-bet -f
```

### 7. 开放阿里云防火墙端口

在阿里云轻量服务器控制台打开防火墙，添加入方向规则：

- 端口：`8000`
- 协议：`TCP`
- 来源：`0.0.0.0/0`

然后访问：

```text
http://你的服务器公网IP:8000
```

### 8. 更新代码后重启

```bash
cd /opt/best-bet
git pull
.venv/bin/pip install -r requirements.txt
systemctl restart best-bet
```

## 可选：绑定域名

如果你要用自己的域名访问大陆服务器，通常需要先做 ICP 备案。只用公网 IP 访问则不需要备案。

## 免费推荐：Railway

1. 打开 Railway，选择 New Project。
2. 选择 Deploy from GitHub repo。
3. 选择仓库：`youjia995-web/best-bet`。
4. 部署后进入服务的 Variables，添加：
   - `ADMIN_PASSWORD`：后台操作密码。
   - `ODDS_API_IO_KEY`：备用接口 key；没有也可以先留空。
   - `DATA_DIR=/var/data`
5. 进入服务的 Volumes，添加一个 Volume，挂载路径填写：`/var/data`。
6. 进入 Networking，生成公开访问域名。

Railway 会读取仓库里的 `railway.json`，启动命令是：

```bash
uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
```

注意：Railway 免费额度有限，Free 计划每月有少量免费 credit，并支持 0.5GB Volume。如果采集频率过高或服务一直运行，可能用完额度后停止服务。

## Render 部署

Render 免费 Web Service 可以运行这个项目，但不能挂载持久磁盘，所以数据库和状态文件可能在重启或重新部署后丢失，而且免费服务空闲后会休眠。它适合演示，不适合长期保存数据。

1. 打开 Render Dashboard，选择 New > Blueprint。
2. 连接 GitHub 仓库：`https://github.com/youjia995-web/best-bet`。
3. 选择仓库根目录的 `render.yaml`。
4. 按提示填写环境变量：
   - `ADMIN_PASSWORD`：后台操作密码。
   - `ODDS_API_IO_KEY`：备用接口 key；没有也可以先留空，主接口仍可运行。
5. 创建服务并等待部署完成。

部署后 Render 会生成一个 `https://...onrender.com` 地址，以后每次推送到 GitHub `main` 分支都会自动重新部署。

## 数据保存位置

Railway 上建议配置 Volume，挂载到 `/var/data`，并设置：

- `DATA_DIR=/var/data`
- SQLite 数据库：`/var/data/odds_monitor.db`
- 数据源状态：`/var/data/data_source_state.json`
- Odds-API 状态：`/var/data/odds_api_io_state.json`

本地运行时不需要额外配置，默认仍使用项目目录下的这些文件。
