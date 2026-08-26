"""BLE (Bluetooth Low Energy) label printer plugin.

This plugin renders InvenTree labels to a high-resolution grayscale image and
produces a self-contained HTML "print job" page. The page performs the
rasterization / scaling / offset / protocol encoding entirely in the browser,
so the user can calibrate all print parameters (paper size, offsets, feed,
DPI, protocol, inversion, threshold) live before sending to the printer.

Because BLE printers are connected to the *client* (browser), the actual
transmission happens in the browser via the Web Bluetooth API. The page
enumerates the printer's GATT services / characteristics and auto-selects a
writable characteristic (with a dropdown to override), so it works with any
BLE printer without hardcoded UUIDs.

The Web Bluetooth API requires a secure context, so the InvenTree site must be
served over HTTPS (or localhost) for this to work.
"""

import base64
import io
import json

from django.core.files.base import ContentFile
from django.utils.translation import gettext_lazy as _

from plugin import InvenTreePlugin
from plugin.mixins import LabelPrintingMixin, SettingsMixin


class BLELabelPrinter(LabelPrintingMixin, SettingsMixin, InvenTreePlugin):
    """Label printing plugin for BLE thermal label printers."""

    NAME = 'BLELabelPrinter'
    SLUG = 'ble-label-printer'
    TITLE = _('BLE label printer')
    DESCRIPTION = _(
        'Print labels to a Bluetooth Low Energy label printer via the browser '
        '(Web Bluetooth API)'
    )
    VERSION = '0.4.0'
    AUTHOR = _('InvenTree contributors')

    BLOCKING_PRINT = True

    SETTINGS = {
        'PROTOCOL': {
            'name': _('Printer protocol'),
            'description': _('Default command protocol (overridable on the print page)'),
            'choices': [
                ('ESC/POS', 'ESC/POS'),
                ('TSPL2', 'TSPL2 (TSC)'),
                ('RAW', 'Raw bitmap (no command wrapper)'),
            ],
            'default': 'ESC/POS',
        },
        'DPI': {
            'name': _('Printer DPI'),
            'description': _('Native resolution of the printer (dots per inch)'),
            'validator': int,
            'default': 203,
        },
        'OFFSET_X': {
            'name': _('Horizontal offset [mm]'),
            'description': _('Default horizontal offset. Positive = right.'),
            'validator': float,
            'default': 0.0,
        },
        'OFFSET_Y': {
            'name': _('Vertical offset [mm]'),
            'description': _('Default vertical offset. Positive = down.'),
            'validator': float,
            'default': 0.0,
        },
        'FEED_MM': {
            'name': _('Eject feed [mm]'),
            'description': _(
                'Default paper feed after printing. 0 = auto (form feed, uses the '
                'printer gap sensor to advance to the next label). Set a value > 0 '
                'to feed a fixed distance instead.'
            ),
            'validator': float,
            'default': 0.0,
        },
        'THRESHOLD': {
            'name': _('Binarization threshold'),
            'description': _(
                'Default grayscale threshold (0-255) used to convert the label '
                'to black and white'
            ),
            'validator': int,
            'default': 128,
        },
        'INVERT': {
            'name': _('Invert bitmap'),
            'description': _(
                'Invert black/white (use if the printed label appears inverted)'
            ),
            'validator': bool,
            'default': False,
        },
        'FILL_MODE': {
            'name': _('Fill mode'),
            'description': _(
                'Default mapping of the label image onto the paper'
            ),
            'choices': [
                ('stretch', 'Stretch'),
                ('fit', 'Fit'),
                ('fill', 'Fill'),
                ('center', 'Center'),
                ('tile', 'Tile'),
            ],
            'default': 'stretch',
        },
        'DEVICE_NAME': {
            'name': _('Device name filter (optional)'),
            'description': _(
                'Optional BLE device name prefix used to filter the device '
                'chooser. Leave empty to select any printer.'
            ),
            'default': '',
        },
        'CHUNK_SIZE': {
            'name': _('Write chunk size'),
            'description': _('Number of bytes written per BLE write operation'),
            'validator': int,
            'default': 200,
        },
        'CHUNK_DELAY': {
            'name': _('Inter-chunk delay'),
            'description': _('Delay in milliseconds between BLE write chunks'),
            'validator': int,
            'default': 15,
        },
    }

    def __init__(self):
        """Register mixins and reset state."""
        super().__init__()
        self._labels = []
        self._width_mm = 40.0
        self._height_mm = 30.0

    # --- helpers ---------------------------------------------------------

    def _get_setting(self, key, default=None):
        """Return a plugin setting value, falling back to the default."""
        try:
            value = self.get_setting(key)
        except Exception:
            value = None

        if value is None or value == '':
            value = self.SETTINGS.get(key, {}).get('default', default)

        return value

    def _get_bool(self, key):
        """Return a boolean setting value."""
        from InvenTree.helpers import str2bool

        return str2bool(self._get_setting(key, False))

    def _get_int(self, key, default):
        """Return an integer setting value."""
        try:
            return int(self._get_setting(key, default))
        except (TypeError, ValueError):
            return default

    def _get_float(self, key, default):
        """Return a float setting value."""
        try:
            return float(self._get_setting(key, default))
        except (TypeError, ValueError):
            return default

    # --- label printing mixin hooks --------------------------------------

    def before_printing(self):
        """Reset the list of collected labels."""
        self._labels = []

    def print_label(self, **kwargs):
        """Render a single label to a high-res grayscale image."""
        label = kwargs.get('label_instance')
        instance = kwargs.get('item_instance')

        width_mm = float(kwargs.get('width') or label.width)
        height_mm = float(kwargs.get('height') or label.height)

        png_file = kwargs.get('png_file')

        if png_file is None:
            png_file = self.render_to_png(label, instance, **kwargs)

        if png_file is None:
            raise ValueError(_('Could not render label to image'))

        self._width_mm = width_mm
        self._height_mm = height_mm

        # Grayscale PNG at high resolution; the browser does the scaling,
        # thresholding, offset and protocol encoding for live calibration.
        gray = png_file.convert('L')
        buf = io.BytesIO()
        gray.save(buf, format='PNG')
        img_b64 = base64.b64encode(buf.getvalue()).decode('ascii')

        self._labels.append(
            {
                'filename': kwargs.get('filename', 'label'),
                'image_w': gray.width,
                'image_h': gray.height,
                'image': img_b64,
            }
        )

    def get_generated_file(self, **kwargs):
        """Build the self-contained Web Bluetooth print page."""
        if not self._labels:
            return None

        config = {
            'protocol': self._get_setting('PROTOCOL') or 'ESC/POS',
            'dpi': self._get_int('DPI', 203),
            'width_mm': self._width_mm,
            'height_mm': self._height_mm,
            'offset_x': self._get_float('OFFSET_X', 0.0),
            'offset_y': self._get_float('OFFSET_Y', 0.0),
            'feed_mm': self._get_float('FEED_MM', 0.0),
            'threshold': self._get_int('THRESHOLD', 128),
            'invert': self._get_bool('INVERT'),
            'fill_mode': self._get_setting('FILL_MODE') or 'stretch',
            'deviceName': self._get_setting('DEVICE_NAME') or '',
            'chunkSize': self._get_int('CHUNK_SIZE', 200),
            'chunkDelay': self._get_int('CHUNK_DELAY', 15),
        }

        html = BLE_PRINT_PAGE.replace('__BLE_CONFIG__', self._json_safe(config))
        html = html.replace('__BLE_LABELS__', self._json_safe(self._labels))

        return ContentFile(html.encode('utf-8'), name='ble-labels.html')

    @staticmethod
    def _json_safe(data):
        """Serialize to JSON, safe for embedding inside a <script> tag."""
        return json.dumps(data).replace('<', '\\u003c')


# --------------------------------------------------------------------------
# Self-contained Web Bluetooth print page
# --------------------------------------------------------------------------

BLE_PRINT_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BLE 标签打印</title>
<style>
  :root { font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
  body { margin: 0; padding: 24px; background: #f5f6f8; color: #1f2430; }
  .card { background: #fff; border-radius: 10px; padding: 20px; margin-bottom: 16px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .muted { color: #6b7280; font-size: 13px; }
  .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; }
  .grid label { font-size: 13px; color: #374151; display: flex; flex-direction: column; gap: 4px; }
  .grid input, .grid select { padding: 8px; border: 1px solid #d1d5db; border-radius: 8px; font-size: 14px; }
  button { border: 0; border-radius: 8px; padding: 10px 18px; font-size: 15px;
           cursor: pointer; font-weight: 600; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  #connect { background: #2563eb; color: #fff; }
  #print { background: #16a34a; color: #fff; }
  #disconnect { background: #e5e7eb; color: #111; }
  #test-feed { background: #f59e0b; color: #fff; }
  #save-params { background: #6b7280; color: #fff; }
  input[type=number] { width: 100%; }
  .labels { display: flex; flex-wrap: wrap; gap: 16px; }
  .label { text-align: center; }
  .label img { border: 1px solid #d1d5db; background: #fff;
               width: 240px; height: auto; border-radius: 4px; }
  .label .name { font-size: 12px; margin-top: 6px; color: #374151; }
  #status { font-size: 13px; color: #374151; white-space: pre-wrap; margin-top: 8px; }
  #status.error { color: #b91c1c; }
  #status.ok { color: #15803d; }
  .check { flex-direction: row !important; align-items: center; gap: 6px !important; }
</style>
</head>
<body>
  <div class="card">
    <h1>BLE 标签打印</h1>
    <div class="muted">连接打印机后，可先「测试走纸」确认连接；调整下方参数会实时更新预览。</div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between;">
      <h1 style="font-size:16px;">打印参数</h1>
      <button id="save-params">保存参数</button>
    </div>
    <div class="grid">
      <label>纸张宽 (mm) <input id="p-width" type="number" step="0.1"></label>
      <label>纸张高 (mm) <input id="p-height" type="number" step="0.1"></label>
      <label>水平偏移 (mm) <input id="p-offx" type="number" step="0.1"></label>
      <label>垂直偏移 (mm) <input id="p-offy" type="number" step="0.1"></label>
      <label>走纸 (mm, 0=间隙自动) <input id="p-feed" type="number" step="0.1"></label>
      <label>DPI <input id="p-dpi" type="number"></label>
      <label>阈值 <input id="p-thr" type="number"></label>
      <label>协议
        <select id="p-proto">
          <option value="ESC/POS">ESC/POS</option>
          <option value="TSPL2">TSPL2</option>
          <option value="RAW">RAW</option>
        </select>
      </label>
      <label>填充方式
        <select id="p-fill">
          <option value="stretch">拉伸</option>
          <option value="fit">适应</option>
          <option value="fill">填充</option>
          <option value="center">居中</option>
          <option value="tile">平铺</option>
        </select>
      </label>
      <label class="check"><input id="p-invert" type="checkbox"> 反色</label>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <button id="connect">连接打印机</button>
      <button id="disconnect" disabled>断开连接</button>
      <span id="dev-status" class="muted">未连接</span>
    </div>
    <div class="row" style="margin-top:12px;">
      <label>写入特征</label>
      <select id="char-select" disabled style="flex:1; min-width:260px;">
        <option value="">（连接后自动选择）</option>
      </select>
      <button id="test-feed" disabled>测试走纸</button>
    </div>
    <div class="row" style="margin-top:12px;">
      <label>份数 <input id="copies" type="number" min="1" value="1" style="width:70px;"></label>
      <button id="print" disabled>打印</button>
    </div>
    <div id="status"></div>
  </div>

  <div class="card">
    <h1 style="font-size:16px;">标签预览</h1>
    <div class="labels" id="preview"></div>
  </div>

<script>
'use strict';

const CONFIG = __BLE_CONFIG__;
const LABELS = __BLE_LABELS__;

const $ = (id) => document.getElementById(id);
const status = (msg, cls) => { const el = $('status'); el.textContent = msg; el.className = cls || ''; };

// ---- initialise parameter inputs from server defaults ----
$('p-width').value = CONFIG.width_mm;
$('p-height').value = CONFIG.height_mm;
$('p-offx').value = CONFIG.offset_x;
$('p-offy').value = CONFIG.offset_y;
$('p-feed').value = CONFIG.feed_mm;
$('p-dpi').value = CONFIG.dpi;
$('p-thr').value = CONFIG.threshold;
$('p-proto').value = CONFIG.protocol;
$('p-invert').checked = !!CONFIG.invert;
$('p-fill').value = CONFIG.fill_mode || 'stretch';

// Restore previously saved parameters (client-side persistence)
loadSavedParams();

function getParams() {
  return {
    width_mm: parseFloat($('p-width').value) || CONFIG.width_mm,
    height_mm: parseFloat($('p-height').value) || CONFIG.height_mm,
    offset_x: parseFloat($('p-offx').value) || 0,
    offset_y: parseFloat($('p-offy').value) || 0,
    feed_mm: parseFloat($('p-feed').value) || 0,
    dpi: parseInt($('p-dpi').value) || 203,
    threshold: parseInt($('p-thr').value) || 128,
    invert: $('p-invert').checked,
    protocol: $('p-proto').value,
    fill: $('p-fill').value
  };
}

function loadSavedParams() {
  try {
    const raw = localStorage.getItem('ble-label-printer-params');
    if (!raw) return false;
    const s = JSON.parse(raw);
    if (!s || typeof s !== 'object') return false;
    if (s.width_mm != null) $('p-width').value = s.width_mm;
    if (s.height_mm != null) $('p-height').value = s.height_mm;
    if (s.offset_x != null) $('p-offx').value = s.offset_x;
    if (s.offset_y != null) $('p-offy').value = s.offset_y;
    if (s.feed_mm != null) $('p-feed').value = s.feed_mm;
    if (s.dpi != null) $('p-dpi').value = s.dpi;
    if (s.threshold != null) $('p-thr').value = s.threshold;
    if (s.invert != null) $('p-invert').checked = !!s.invert;
    if (s.protocol) $('p-proto').value = s.protocol;
    if (s.fill) $('p-fill').value = s.fill;
    return true;
  } catch (e) {
    return false;
  }
}

async function saveParams() {
  const p = getParams();
  // Always persist to this browser
  try {
    localStorage.setItem('ble-label-printer-params', JSON.stringify(p));
  } catch (e) {}

  // Also persist to the plugin settings (server side)
  const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
  const map = {
    PROTOCOL: p.protocol,
    DPI: String(p.dpi),
    OFFSET_X: String(p.offset_x),
    OFFSET_Y: String(p.offset_y),
    FEED_MM: String(p.feed_mm),
    THRESHOLD: String(p.threshold),
    INVERT: p.invert ? 'True' : 'False',
    FILL_MODE: p.fill
  };

  let ok = true;
  for (const [key, value] of Object.entries(map)) {
    try {
      const resp = await fetch('/api/plugins/ble-label-printer/settings/' + key + '/', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify({ value: value })
      });
      if (!resp.ok) ok = false;
    } catch (e) {
      ok = false;
    }
  }

  status(
    ok
      ? '参数已保存（已持久化到插件设置，下次打印生效）。'
      : '参数已保存到本浏览器；服务器保存失败（请确认已登录）。',
    ok ? 'ok' : ''
  );
}

function normalizeUuid(u) {
  if (!u) return '';
  const hex = String(u).replace(/[^0-9a-fA-F]/g, '').toLowerCase();
  if (hex.length === 32) {
    return hex.slice(0,8) + '-' + hex.slice(8,12) + '-' + hex.slice(12,16) + '-' +
           hex.slice(16,20) + '-' + hex.slice(20);
  }
  if (hex.length === 4) return '0000' + hex + '-0000-1000-8000-00805f9b34fb';
  return String(u).toLowerCase();
}

const COMMON_SERVICES = [
  '0000ffe0-0000-1000-8000-00805f9b34fb',
  '0000ff00-0000-1000-8000-00805f9b34fb',
  '0000fff0-0000-1000-8000-00805f9b34fb',
  '0000ffc0-0000-1000-8000-00805f9b34fb',
  '0000ffd0-0000-1000-8000-00805f9b34fb',
  '0000ffd1-0000-1000-8000-00805f9b34fb',
  '0000ffe5-0000-1000-8000-00805f9b34fb',
  '0000fee7-0000-1000-8000-00805f9b34fb',
  '0000ff10-0000-1000-8000-00805f9b34fb',
  '00001800-0000-1000-8000-00805f9b34fb',
  '00001801-0000-1000-8000-00805f9b34fb',
  '00001811-0000-1000-8000-00805f9b34fb',
  '000018f0-0000-1000-8000-00805f9b34fb'
];

const STANDARD_SERVICES = [
  '00001800-0000-1000-8000-00805f9b34fb',
  '00001801-0000-1000-8000-00805f9b34fb',
  '0000180a-0000-1000-8000-00805f9b34fb',
  '0000180f-0000-1000-8000-00805f9b34fb',
  '00001803-0000-1000-8000-00805f9b34fb',
  '00001802-0000-1000-8000-00805f9b34fb',
  '00001805-0000-1000-8000-00805f9b34fb'
];

function rankChar(c, serviceUuid) {
  const s = normalizeUuid(serviceUuid);
  let rank = 0;
  if (c.properties.writeWithoutResponse && c.properties.write) rank = 30;
  else if (c.properties.writeWithoutResponse) rank = 20;
  else if (c.properties.write) rank = 10;
  if (c.properties.notify) rank += 5;
  if (!STANDARD_SERVICES.includes(s)) rank += 2;
  return rank;
}

// ---- image loading ----
async function loadGrayscale(b64) {
  const img = new Image();
  await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = 'data:image/png;base64,' + b64; });
  const canvas = document.createElement('canvas');
  canvas.width = img.width; canvas.height = img.height;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0);
  const d = ctx.getImageData(0, 0, img.width, img.height).data;
  const gray = new Uint8Array(img.width * img.height);
  for (let i = 0; i < gray.length; i++) gray[i] = d[i * 4];
  return { gray, w: img.width, h: img.height };
}

// ---- rendering ----
function fillMap(px, py, w, h, dotsX, dotsY, mode) {
  let sx, sy;
  if (mode === 'fit') {
    const s = Math.min(dotsX / w, dotsY / h);
    sx = (px - (dotsX - w * s) / 2) / s;
    sy = (py - (dotsY - h * s) / 2) / s;
  } else if (mode === 'fill') {
    const s = Math.max(dotsX / w, dotsY / h);
    sx = (px - (dotsX - w * s) / 2) / s;
    sy = (py - (dotsY - h * s) / 2) / s;
  } else if (mode === 'center') {
    sx = px - (dotsX - w) / 2;
    sy = py - (dotsY - h) / 2;
  } else if (mode === 'tile') {
    sx = ((px % w) + w) % w;
    sy = ((py % h) + h) % h;
  } else {
    // stretch (default)
    sx = px * w / dotsX;
    sy = py * h / dotsY;
  }
  const ix = Math.floor(sx), iy = Math.floor(sy);
  if (ix < 0 || ix >= w || iy < 0 || iy >= h) return null;
  return { sx: ix, sy: iy };
}

function renderRaster(gray, w, h, p) {
  const dotsX = Math.max(1, Math.round(p.width_mm / 25.4 * p.dpi));
  const dotsY = Math.max(1, Math.round(p.height_mm / 25.4 * p.dpi));
  const bytesPerRow = Math.ceil(dotsX / 8);
  const offY = Math.round(p.offset_y / 25.4 * p.dpi);
  const raster = new Uint8Array(bytesPerRow * dotsY);

  for (let y = 0; y < dotsY; y++) {
    const py = y - offY;
    for (let x = 0; x < dotsX; x++) {
      let isWhite = true;
      if (py >= 0 && py < dotsY) {
        const m = fillMap(x, py, w, h, dotsX, dotsY, p.fill);
        if (m) {
          isWhite = gray[m.sy * w + m.sx] >= p.threshold;
        }
      }
      let bit = isWhite ? 0 : 1;
      if (p.invert) bit = 1 - bit;
      if (bit) raster[y * bytesPerRow + (x >> 3)] |= 0x80 >> (x & 7);
    }
  }

  return { raster, dotsX, dotsY, bytesPerRow };
}

function encodeEscPos(raster, dotsX, dotsY, bytesPerRow, feedDots) {
  const xL = bytesPerRow & 0xFF, xH = (bytesPerRow >> 8) & 0xFF;
  const yL = dotsY & 0xFF, yH = (dotsY >> 8) & 0xFF;
  const out = [0x1b, 0x40, 0x1d, 0x76, 0x30, 0x00, xL, xH, yL, yH];
  for (let i = 0; i < raster.length; i++) out.push(raster[i]);
  if (feedDots <= 0) {
    out.push(0x0c);  // FF (form feed): advance to the next label using the gap sensor
  } else {
    let d = feedDots;
    while (d > 0) { const n = Math.min(d, 255); out.push(0x1b, 0x4a, n); d -= n; }
  }
  return new Uint8Array(out);
}

function encodeTspl(raster, dotsX, dotsY, bytesPerRow, p, offX) {
  const header = 'SIZE ' + p.width_mm + ' mm,' + p.height_mm + ' mm\\r\\n' +
                 'GAP 0 mm,0 mm\\r\\nDIRECTION 1\\r\\nCLS\\r\\n' +
                 'BITMAP ' + offX + ',0,' + bytesPerRow + ',' + dotsY + ',0,';
  const hdr = new TextEncoder().encode(header);
  const tail = new TextEncoder().encode('\\r\\nPRINT 1,1\\r\\n');
  const out = new Uint8Array(hdr.length + raster.length + tail.length);
  out.set(hdr, 0);
  out.set(raster, hdr.length);
  out.set(tail, hdr.length + raster.length);
  return out;
}

function padLeft(raster, bytesPerRow, dotsY, offX) {
  const dotsX = bytesPerRow * 8;
  const newDotsX = dotsX + offX;
  const newBytesPerRow = Math.ceil(newDotsX / 8);
  const out = new Uint8Array(newBytesPerRow * dotsY);
  for (let y = 0; y < dotsY; y++) {
    for (let x = 0; x < dotsX; x++) {
      const bit = (raster[y * bytesPerRow + (x >> 3)] >> (7 - (x & 7))) & 1;
      if (bit) {
        const nx = x + offX;
        out[y * newBytesPerRow + (nx >> 3)] |= 0x80 >> (nx & 7);
      }
    }
  }
  return { raster: out, bytesPerRow: newBytesPerRow, dotsX: newDotsX };
}

function encodePayload(r, p) {
  const offX = Math.max(0, Math.round(p.offset_x / 25.4 * p.dpi));
  const proto = (p.protocol || 'ESC/POS').toUpperCase();
  if (proto === 'TSPL2') return encodeTspl(r.raster, r.dotsX, r.dotsY, r.bytesPerRow, p, offX);
  const padded = offX > 0
    ? padLeft(r.raster, r.bytesPerRow, r.dotsY, offX)
    : { raster: r.raster, bytesPerRow: r.bytesPerRow, dotsX: r.dotsX };
  if (proto === 'RAW') return padded.raster;
  const feedDots = Math.round(p.feed_mm / 25.4 * p.dpi);
  return encodeEscPos(padded.raster, padded.dotsX, r.dotsY, padded.bytesPerRow, feedDots);
}

// ---- state ----
let device = null, selectedChar = null, writableChars = [];
let loadedLabels = [];   // [{filename, gray, w, h}]
let rendered = [];       // [{raster, dotsX, dotsY, bytesPerRow, payload}]

function refresh() {
  const p = getParams();
  rendered = loadedLabels.map(l => {
    const r = renderRaster(l.gray, l.w, l.h, p);
    r.payload = encodePayload(r, p);
    return r;
  });
}

// Preview shows the original InvenTree-rendered label (not the calibrated output)
function renderPreviews() {
  const wrap = $('preview');
  wrap.innerHTML = '';
  loadedLabels.forEach((l, i) => {
    const box = document.createElement('div');
    box.className = 'label';
    const img = document.createElement('img');
    img.src = 'data:image/png;base64,' + l.b64;
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = (i + 1) + '. ' + l.filename;
    box.appendChild(img);
    box.appendChild(name);
    wrap.appendChild(box);
  });
}

// ---- BLE ----
async function connect() {
  if (!navigator.bluetooth) {
    status('此浏览器不支持 Web Bluetooth，请使用 Chrome/Edge 并通过 HTTPS 访问。', 'error');
    return;
  }
  try {
    const options = { acceptAllDevices: true, optionalServices: COMMON_SERVICES };
    if (CONFIG.deviceName) {
      options.filters = [{ namePrefix: CONFIG.deviceName }];
      delete options.acceptAllDevices;
    }
    device = await navigator.bluetooth.requestDevice(options);
    $('dev-status').textContent = '连接中: ' + (device.name || device.id);
    const server = await device.gatt.connect();
    device.addEventListener('gattserverdisconnected', () => {
      $('dev-status').textContent = '已断开';
      $('print').disabled = true;
      $('test-feed').disabled = true;
      $('connect').disabled = false;
      $('disconnect').disabled = true;
    });

    const services = await server.getPrimaryServices();
    writableChars = [];
    for (const s of services) {
      let chars = [];
      try { chars = await s.getCharacteristics(); } catch (e) { chars = []; }
      for (const c of chars) {
        if (c.properties.writeWithoutResponse || c.properties.write) {
          writableChars.push({ service: s.uuid, char: c.uuid, characteristic: c, rank: rankChar(c, s.uuid) });
        }
      }
    }
    writableChars.sort((a, b) => b.rank - a.rank);

    const sel = $('char-select');
    sel.innerHTML = '';
    if (writableChars.length === 0) {
      const o = document.createElement('option');
      o.value = ''; o.textContent = '未找到可写特征';
      sel.appendChild(o);
      selectedChar = null;
    } else {
      writableChars.forEach((w, i) => {
        const o = document.createElement('option');
        o.value = String(i);
        o.textContent = w.service + '  →  ' + w.char;
        sel.appendChild(o);
      });
      sel.value = '0';
      selectedChar = writableChars[0].characteristic;
    }

    $('dev-status').textContent = '已连接: ' + (device.name || device.id) + ' (id: ' + device.id + ')';
    $('connect').disabled = true;
    $('disconnect').disabled = false;
    $('char-select').disabled = false;
    $('print').disabled = !selectedChar;
    $('test-feed').disabled = !selectedChar;
    status(selectedChar ? '已连接。可先「测试走纸」确认，或切换「写入特征」。' : '未找到可写特征。', selectedChar ? 'ok' : 'error');
  } catch (e) {
    $('dev-status').textContent = '未连接';
    status('连接失败: ' + (e.message || e), 'error');
  }
}

async function disconnect() {
  if (device && device.gatt.connected) device.gatt.disconnect();
  $('dev-status').textContent = '未连接';
  $('connect').disabled = false;
  $('disconnect').disabled = true;
  $('char-select').disabled = true;
  $('print').disabled = true;
  $('test-feed').disabled = true;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function writeBytes(data) {
  const chunk = Math.max(16, CONFIG.chunkSize || 200);
  for (let i = 0; i < data.length; i += chunk) {
    const part = data.slice(i, i + chunk);
    if (selectedChar.properties.writeWithoutResponse) {
      await selectedChar.writeValueWithoutResponse(part);
    } else {
      await selectedChar.writeValue(part);
    }
    if (CONFIG.chunkDelay > 0) await sleep(CONFIG.chunkDelay);
  }
}

async function testFeed() {
  if (!selectedChar) { status('请先连接打印机。', 'error'); return; }
  try {
    const p = getParams();
    const out = [0x1b, 0x40];  // ESC @ init
    let msg;
    if (p.feed_mm <= 0) {
      out.push(0x0c);  // FF: advance to the next label (gap sensor)
      msg = '已发送走纸指令（间隙检测自动走纸），观察打印机是否走到下一张标签。';
    } else {
      const feedDots = Math.round(p.feed_mm / 25.4 * p.dpi);
      let d = feedDots;
      while (d > 0) { const n = Math.min(d, 255); out.push(0x1b, 0x4a, n); d -= n; }
      msg = '已发送走纸指令（' + p.feed_mm + ' mm），观察打印机是否走纸。';
    }
    await writeBytes(new Uint8Array(out));
    status(msg, 'ok');
  } catch (e) {
    status('走纸指令发送失败: ' + (e.message || e), 'error');
  }
}

async function print() {
  if (!selectedChar) { status('请先连接打印机。', 'error'); return; }
  const copies = Math.max(1, parseInt($('copies').value || '1', 10));
  const total = rendered.length * copies;
  let done = 0;
  try {
    $('print').disabled = true;
    for (let c = 0; c < copies; c++) {
      for (const r of rendered) {
        status('正在打印 ' + (done + 1) + '/' + total + ' ...');
        await writeBytes(r.payload);
        done++;
      }
    }
    status('打印完成，共 ' + total + ' 张标签。', 'ok');
  } catch (e) {
    status('打印出错: ' + (e.message || e), 'error');
  } finally {
    $('print').disabled = false;
  }
}

// ---- wire up events ----
$('connect').addEventListener('click', connect);
$('disconnect').addEventListener('click', disconnect);
$('print').addEventListener('click', print);
$('test-feed').addEventListener('click', testFeed);
$('save-params').addEventListener('click', saveParams);
$('char-select').addEventListener('change', (ev) => {
  const idx = parseInt(ev.target.value, 10);
  if (!isNaN(idx) && writableChars[idx]) selectedChar = writableChars[idx].characteristic;
});

['p-width', 'p-height', 'p-offx', 'p-offy', 'p-feed', 'p-dpi', 'p-thr', 'p-proto', 'p-fill'].forEach(id => {
  $(id).addEventListener('input', refresh);
});
$('p-invert').addEventListener('change', refresh);

// ---- init ----
(async function init() {
  for (const l of LABELS) {
    try {
      const g = await loadGrayscale(l.image);
      loadedLabels.push({ filename: l.filename, b64: l.image, gray: g.gray, w: g.w, h: g.h });
    } catch (e) {
      status('标签图像加载失败: ' + (e.message || e), 'error');
    }
  }
  renderPreviews();
  refresh();
})();
</script>
</body>
</html>
"""
