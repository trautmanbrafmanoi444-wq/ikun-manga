# 部署到 2C1G 服务器

纯 Python 标准库，不装 pip 依赖。2C1G 够跑首页、在线阅读和图片代理。别同时开很多「下载这一话」（zip 会占内存）。

## 1. 上传代码

把整个 `exports/baozimh` 目录拷到服务器，例如 `/opt/ikun`：

```bash
rsync -av --exclude data --exclude static/index.orig.html \
  /Users/niaoniao/Documents/grok/exports/baozimh/ \
  root@你的服务器IP:/opt/ikun/
```

服务器上：

```bash
apt-get update
apt-get install -y python3 nginx
mkdir -p /opt/ikun/data
chown -R www-data:www-data /opt/ikun
```

## 2. systemd 拉起

```bash
cp /opt/ikun/deploy/ikun.service /etc/systemd/system/ikun.service
systemctl daemon-reload
systemctl enable --now ikun
curl -s http://127.0.0.1:8788/health
```

应返回 `ok`。

直连试（没 nginx 时）：

```bash
# 改服务环境 HOST=0.0.0.0 后
systemctl restart ikun
# 浏览器打开 http://服务器IP:8788
```

## 3. nginx 反代 80 端口

```bash
cp /opt/ikun/deploy/nginx.conf /etc/nginx/sites-available/ikun
ln -sf /etc/nginx/sites-available/ikun /etc/nginx/sites-enabled/ikun
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

然后打开 `http://服务器IP`。有域名再套 certbot。

## 4. 本机临时公网（调试）

```bash
HOST=0.0.0.0 PORT=8788 python3 server.py
```

## 资源占用

- CPU：解码是毫秒级，主要耗在代理 webp
- 内存：空闲几十 MB；阅读走流，不落原图
- 磁盘：`data/` 只缓存 JSON，可定期清
