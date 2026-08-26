# BLE 标签打印机插件

通过浏览器 **Web Bluetooth** 连接 BLE 热敏标签打印机，在 InvenTree 零件页面选中零件即可打印标签纸（默认 40mm × 30mm）。

## 特性

- **通用 BLE 打印机**：连接后自动枚举设备的 GATT 服务/特征并选中可写特征，无需硬编码 UUID（也提供下拉框手动切换）。
- **多协议**：ESC/POS（默认）、TSPL2、RAW 位图。
- **打印页内实时校准**：纸张宽/高(mm)、水平/垂直偏移(mm)、走纸(mm)、DPI、阈值、反色、协议，均可直接在打印页调整。
- **间隙检测走纸**：走纸 = 0 时发送 `FF`（换页），由打印机间隙传感器自动定位到下一张标签；也可设手动走纸 mm。
- **偏移仅作用于实际打印**（补偿纸张安装差异），预览始终显示 InvenTree 渲染的原始标签样式。

## 安装（Docker 部署）

1. 将本目录复制到 InvenTree 数据卷的 `plugins/` 目录下：

   ```bash
   cp -r ble_label_printer /www/inventree/plugins/
   ```

2. 在 InvenTree 后台 **设置 → 插件** 中启用 **`BLE label printer`**。

## HTTPS 要求（重要）

Web Bluetooth API 仅在安全上下文（HTTPS 或 localhost）中可用。需要在反向代理上启用 HTTPS（自签名证书即可，浏览器首次访问手动信任）。

nginx 关键配置（在 `inventree-proxy` 上新增 443 监听，并让 `/media/*.html` 内联渲染，因为打印页是作为数据输出返回的 HTML）：

```nginx
server {
    listen 443 ssl;
    ssl_certificate     /etc/nginx/certs/cert.pem;
    ssl_certificate_key /etc/nginx/certs/key.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    location / { proxy_pass http://inventree-server:8000; }

    # 打印页为 /media/data_output/*.html，需内联渲染而非强制下载
    location ~ ^/media/.*\.html$ {
        root /var/www;
        auth_request /auth;
        default_type text/html;
        charset utf-8;
    }

    location /media/ { alias /var/www/media/; auth_request /auth; }
    location /static/ { alias /var/www/static/; }
}
```

同时在 `.env` 中设置 `INVENTREE_SITE_URL` 为 HTTPS 地址，并将 `https://...` 加入 `INVENTREE_TRUSTED_ORIGINS`。

## 标签模板

`ble_part_label.html` 是默认零件标签模板（40mm × 30mm，含名称/IPN/描述/二维码）。实际使用中在 InvenTree 后台 **标签模板** 里按需自定义；纸张尺寸由模板宽高决定，打印页上可二次微调。

## 使用

1. 零件页面选中零件 → **打印标签**。
2. 选择标签模板与 **`BLE label printer`** 插件 → **打印**。
3. 新打开的打印页中：**连接打印机** → **测试走纸**（确认连接）→ **打印**。
4. 若打印偏移/尺寸不符，在打印页「打印参数」中调整水平/垂直偏移或纸张宽高。

## 插件设置（默认值，可在打印页覆盖）

| 设置 | 默认 | 说明 |
|---|---|---|
| Printer protocol | ESC/POS | ESC/POS / TSPL2 / RAW |
| Printer DPI | 203 | 打印分辨率 |
| Horizontal offset [mm] | 0 | 水平偏移，正=右移 |
| Vertical offset [mm] | 0 | 垂直偏移，正=下移 |
| Eject feed [mm] | 0 | 0=间隙自动（FF），>0=手动走纸 |
| Binarization threshold | 128 | 灰度二值化阈值 |
| Invert bitmap | False | 黑白反转 |
| Write chunk size | 200 | 每次 BLE 写入字节数 |
| Inter-chunk delay | 15 | 写入块间延迟(ms) |
