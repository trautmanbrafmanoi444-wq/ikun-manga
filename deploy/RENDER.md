# 部署到 Render

站点是纯 Python，无 pip 依赖。Render 免费 Web Service 即可。

## 一次推 GitHub

在本机（仓库根就是 `exports/baozimh`）：

```bash
cd /Users/niaoniao/Documents/grok/exports/baozimh
git init
git add server.py decoder.py requirements.txt runtime.txt render.yaml static/index.html notes.md deploy .gitignore
git commit -m "i kun manga"
```

到 GitHub 新建空仓库，例如 `ikun-manga`，然后：

```bash
git remote add origin https://github.com/你的用户名/ikun-manga.git
git branch -M main
git push -u origin main
```

不要把 `data/` 推上去。

## Render 控制台

1. 打开 https://dashboard.render.com
2. **New + → Web Service → 选刚推的仓库**
3. 填：
   - **Name**: `ikun-manga`
   - **Region**: 选离你近的（Singapore / Oregon）
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python server.py`
   - **Instance**: Free
4. Advanced：
   - Health Check Path: `/health`
   - 环境变量 `HOST=0.0.0.0`（`PORT` 不用填，平台会注入）
5. **Create Web Service**

部署成功后地址类似 `https://ikun-manga.onrender.com`

仓库里有 `render.yaml` 的话，也可以 **New + → Blueprint**，选仓库自动建服务。

## 不休眠

免费实例闲置约 15 分钟会睡，第一次打开要等 30～60 秒。

- **真正常开**：Render 控制台点 **Upgrade now**，升到 Starter（约 $7/月）。这是官方方案。
- **免费保活**：仓库里 `.github/workflows/keepalive.yml` 每 10 分钟访问 `/health`。GitHub cron 可能晚点，偶尔仍会睡。保活会把免费小时打满。

## 免费套餐注意

- 15 分钟没人访问会休眠，第一次打开要等 30～60 秒
- 出口 IP 可能被 Cloudflare / 图床 CDN 拦，首页或图片偶发 502
- 磁盘不持久，`data/` 缓存重启会丢，不影响阅读
- 不要多人同时点「下载这一话」（zip 吃内存）

本地仍是：

```bash
python3 server.py
# http://127.0.0.1:8788
```
