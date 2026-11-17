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

Multi-label jobs
----------------
The plugin used to emit ``ESC @`` (reset) before *every* label.  BLE writes
are fast relative to the printer's mechanics, so the reset of label N+1 often
reached the printer while label N was still being printed.  ESC/POS ``ESC @``
clears the print buffer, so the tail of label N was discarded and the FF that
followed ejected a half-printed label - exactly the "first label fine, every
following label truncated" symptom.

The printer page now:

* sends ``ESC @`` **once per job** (optional legacy per-label reset),
* uses acknowledged GATT writes (``writeValue``) by default, which pace the
  host to the printer's buffer instead of fire-and-forget
  ``writeValueWithoutResponse``,
* waits between labels and can split the bitmap into bands, and
* logs per-label byte counts / durations so future issues can be diagnosed.

The protocol/transport logic lives in :data:`BLE_CORE_JS`, a pure JavaScript
module that is embedded into the generated page.  It has no DOM dependency and
is covered by the Node test-suite in ``tests/``.
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
    VERSION = '0.5.2'
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
        'GAP_MM': {
            'name': _('Label gap [mm]'),
            'description': _(
                'Gap between die-cut labels. Only used by the TSPL2 protocol '
                '(GAP command). ESC/POS uses the printer gap sensor via FF.'
            ),
            'validator': float,
            'default': 2.0,
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
        'WRITE_MODE': {
            'name': _('BLE write mode'),
            'description': _(
                'How data is written to the printer. "Auto" prefers acknowledged '
                'writes (write with response), which provide flow control and '
                'prevent dropped bytes on multi-label jobs. Use "noack" only if '
                'the printer rejects write requests.'
            ),
            'choices': [
                ('auto', 'Auto (prefer acknowledged)'),
                ('ack', 'Acknowledged (write with response)'),
                ('noack', 'Write without response'),
            ],
            'default': 'auto',
        },
        'WRITE_CHUNK_SIZE': {
            'name': _('Write chunk size'),
            'description': _(
                'Number of bytes written per BLE write operation. 180 is a safe '
                'default for both acknowledged and unacknowledged writes.'
            ),
            'validator': int,
            'default': 180,
        },
        'WRITE_CHUNK_DELAY': {
            'name': _('Inter-chunk delay'),
            'description': _(
                'Delay in milliseconds between BLE write chunks. The acknowledged '
                'mode already adds the GATT round-trip time; 15 ms is a safe margin '
                'for slow printer firmware.'
            ),
            'validator': int,
            'default': 15,
        },
        'INTER_LABEL_DELAY': {
            'name': _('Inter-label delay [ms]'),
            'description': _(
                'Delay between two labels. Gives the printer time to finish '
                'printing / feeding before the next bitmap is pushed. Increase '
                'this if only the first label of a job prints completely.'
            ),
            'validator': int,
            'default': 800,
        },
        'BAND_ROWS': {
            'name': _('Band height [rows]'),
            'description': _(
                'Split each label bitmap into bands of this many rows '
                '(0 = send the whole label as one bitmap). Useful for printers '
                'with a very small receive buffer.'
            ),
            'validator': int,
            'default': 0,
        },
        'RESET_EACH_LABEL': {
            'name': _('Reset before every label'),
            'description': _(
                'Send ESC @ before each label (legacy behaviour). Usually NOT '
                'needed and known to truncate multi-label jobs, because the '
                'reset can reach the printer while the previous label is still '
                'printing.'
            ),
            'validator': bool,
            'default': False,
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
            'gap_mm': self._get_float('GAP_MM', 2.0),
            'threshold': self._get_int('THRESHOLD', 128),
            'invert': self._get_bool('INVERT'),
            'fill_mode': self._get_setting('FILL_MODE') or 'stretch',
            'deviceName': self._get_setting('DEVICE_NAME') or '',
            'write_mode': self._get_setting('WRITE_MODE') or 'auto',
            'chunk_size': self._get_int('WRITE_CHUNK_SIZE', 180),
            'chunk_delay': self._get_int('WRITE_CHUNK_DELAY', 15),
            'inter_label_delay': self._get_int('INTER_LABEL_DELAY', 800),
            'band_rows': self._get_int('BAND_ROWS', 0),
            'reset_each_label': self._get_bool('RESET_EACH_LABEL'),
        }

        html = BLE_PRINT_PAGE.replace('__BLE_CORE_JS__', BLE_CORE_JS)
        html = html.replace('__BLE_CONFIG__', self._json_safe(config))
        html = html.replace('__BLE_LABELS__', self._json_safe(self._labels))

        return ContentFile(html.encode('utf-8'), name='ble-labels.html')

    @staticmethod
    def _json_safe(data):
        """Serialize to JSON, safe for embedding inside a <script> tag."""
        return json.dumps(data).replace('<', '\\u003c')


# --------------------------------------------------------------------------
# Protocol / transport core (pure JavaScript, no DOM dependency)
# --------------------------------------------------------------------------

BLE_CORE_JS = r"""
(function (root) {
  'use strict';

  const ESC = 0x1b, GS = 0x1d, FF = 0x0c;

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, Math.max(0, ms | 0)); });
  }

  // ---- image mapping ---------------------------------------------------
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
    const offY = Math.round((p.offset_y || 0) / 25.4 * p.dpi);
    const raster = new Uint8Array(bytesPerRow * dotsY);

    for (let y = 0; y < dotsY; y++) {
      const py = y - offY;
      for (let x = 0; x < dotsX; x++) {
        let isWhite = true;
        if (py >= 0 && py < dotsY) {
          const m = fillMap(x, py, w, h, dotsX, dotsY, p.fill);
          if (m) isWhite = gray[m.sy * w + m.sx] >= p.threshold;
        }
        let bit = isWhite ? 0 : 1;
        if (p.invert) bit = 1 - bit;
        if (bit) raster[y * bytesPerRow + (x >> 3)] |= 0x80 >> (x & 7);
      }
    }

    return { raster: raster, dotsX: dotsX, dotsY: dotsY, bytesPerRow: bytesPerRow };
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

  // ---- ESC/POS ---------------------------------------------------------
  function escPosBitmap(raster, bytesPerRow, rowStart, rows) {
    const size = 8 + bytesPerRow * rows;
    const out = new Uint8Array(size);
    out[0] = GS; out[1] = 0x76; out[2] = 0x30; out[3] = 0x00;  // GS v 0 m
    out[4] = bytesPerRow & 0xff;
    out[5] = (bytesPerRow >> 8) & 0xff;
    out[6] = rows & 0xff;
    out[7] = (rows >> 8) & 0xff;
    out.set(raster.subarray(rowStart * bytesPerRow, (rowStart + rows) * bytesPerRow), 8);
    return out;
  }

  function escPosFeedSegments(feedDots) {
    if (feedDots <= 0) return [Uint8Array.of(FF)];
    const segs = [];
    let d = feedDots;
    while (d > 0) {
      const n = Math.min(d, 255);
      segs.push(Uint8Array.of(ESC, 0x4a, n));  // ESC J n
      d -= n;
    }
    return segs;
  }

  function buildEscPosLabel(r, p) {
    const offX = Math.max(0, Math.round((p.offset_x || 0) / 25.4 * p.dpi));
    const padded = offX > 0
      ? padLeft(r.raster, r.bytesPerRow, r.dotsY, offX)
      : { raster: r.raster, bytesPerRow: r.bytesPerRow, dotsX: r.dotsX };

    const bandRows = Math.max(0, Math.round(p.band_rows || 0));
    const segs = [];
    if (bandRows > 0 && bandRows < r.dotsY) {
      for (let y = 0; y < r.dotsY; y += bandRows) {
        segs.push(escPosBitmap(padded.raster, padded.bytesPerRow, y,
          Math.min(bandRows, r.dotsY - y)));
      }
    } else {
      segs.push(escPosBitmap(padded.raster, padded.bytesPerRow, 0, r.dotsY));
    }
    const feedDots = Math.round((p.feed_mm || 0) / 25.4 * p.dpi);
    Array.prototype.push.apply(segs, escPosFeedSegments(feedDots));
    return segs;
  }

  // ---- TSPL2 -----------------------------------------------------------
  function buildTsplLabel(r, p) {
    const offX = Math.max(0, Math.round((p.offset_x || 0) / 25.4 * p.dpi));
    const gap = (p.gap_mm === undefined || p.gap_mm === null) ? 2 : p.gap_mm;
    const header = 'SIZE ' + p.width_mm + ' mm,' + p.height_mm + ' mm\r\n' +
                   'GAP ' + gap + ' mm,0 mm\r\nDIRECTION 1\r\nCLS\r\n' +
                   'BITMAP ' + offX + ',0,' + r.bytesPerRow + ',' + r.dotsY + ',0,';
    const hdr = new TextEncoder().encode(header);
    const tail = new TextEncoder().encode('\r\nPRINT 1,1\r\n');
    const out = new Uint8Array(hdr.length + r.raster.length + tail.length);
    out.set(hdr, 0);
    out.set(r.raster, hdr.length);
    out.set(tail, hdr.length + r.raster.length);
    return out;
  }

  // ---- RAW -------------------------------------------------------------
  function buildRawLabel(r, p) {
    // A raw bitmap still needs a form feed, otherwise consecutive labels are
    // printed as one endless bitmap.
    const offX = Math.max(0, Math.round((p.offset_x || 0) / 25.4 * p.dpi));
    const padded = offX > 0
      ? padLeft(r.raster, r.bytesPerRow, r.dotsY, offX)
      : { raster: r.raster };
    return [padded.raster, Uint8Array.of(FF)];
  }

  // ---- job assembly ----------------------------------------------------
  function buildJob(rasters, p) {
    const proto = String(p.protocol || 'ESC/POS').toUpperCase();
    const labels = rasters.map(function (r) {
      if (proto === 'TSPL2') return [buildTsplLabel(r, p)];
      if (proto === 'RAW') return buildRawLabel(r, p);
      return buildEscPosLabel(r, p);
    });
    // ESC @ is sent once per job.  Never inject it between labels: it clears
    // the print buffer and would truncate the label currently being printed.
    const init = proto === 'TSPL2' ? new Uint8Array(0) : Uint8Array.of(ESC, 0x40);
    return { init: init, labels: labels, protocol: proto };
  }

  function jobBytes(job) {
    let n = job.init ? job.init.length : 0;
    job.labels.forEach(function (segs) {
      segs.forEach(function (s) { n += s.length; });
    });
    return n;
  }

  // ---- BLE transport ---------------------------------------------------
  function resolveWriteMode(char, requested) {
    const props = (char && char.properties) || {};
    const canWrite = !!props.write;
    const canNoAck = !!props.writeWithoutResponse;
    const req = String(requested || 'auto').toLowerCase();
    if (req === 'ack') return canWrite ? 'ack' : 'noack';
    if (req === 'noack') return canNoAck ? 'noack' : 'ack';
    return canWrite ? 'ack' : 'noack';
  }

  async function writeData(char, data, opts) {
    const mode = opts.mode;
    const chunk = Math.max(16, Math.min(512, opts.chunkSize || 180));
    const delay = Math.max(0, opts.delay | 0);
    let sent = 0;
    for (let i = 0; i < data.length; i += chunk) {
      const part = data.subarray(i, i + chunk);
      if (mode === 'noack') {
        await char.writeValueWithoutResponse(part);
      } else {
        await char.writeValue(part);
      }
      sent += part.length;
      if (delay > 0 && i + chunk < data.length) await sleep(delay);
    }
    return sent;
  }

  async function sendJob(spec) {
    const char = spec.char;
    const job = spec.job;
    const p = spec.p || {};
    const onProgress = spec.onProgress;
    const mode = resolveWriteMode(char, p.write_mode);
    let completedLabels = 0;

    const run = async function (writeMode) {
      // Without ACKs there is no flow control at all, so use a conservative
      // chunk size / delay even if the user configured something faster.
      const noAck = writeMode === 'noack';
      const wopts = {
        mode: writeMode,
        chunkSize: noAck ? Math.min(p.chunk_size || 180, 100) : p.chunk_size,
        delay: noAck ? Math.max(p.chunk_delay || 0, 30) : p.chunk_delay
      };
      const t0 = Date.now();
      let total = 0;

      if (!p.reset_each_label && job.init && job.init.length) {
        total += await writeData(char, job.init, wopts);
        await sleep(80);  // let the printer settle after ESC @
      }

      for (let i = 0; i < job.labels.length; i++) {
        const labelStart = Date.now();
        if (p.reset_each_label && job.init && job.init.length) {
          total += await writeData(char, job.init, wopts);
          await sleep(80);
        }
        let labelBytes = 0;
        const segs = job.labels[i];
        for (let s = 0; s < segs.length; s++) {
          labelBytes += await writeData(char, segs[s], wopts);
        }
        total += labelBytes;
        completedLabels = i + 1;
        if (onProgress) {
          onProgress(i + 1, job.labels.length, {
            bytes: labelBytes,
            ms: Date.now() - labelStart
          });
        }
        if (i < job.labels.length - 1) {
          await sleep(Math.max(0, p.inter_label_delay | 0));
        }
      }

      return { bytes: total, ms: Date.now() - t0, mode: writeMode };
    };

    try {
      return await run(mode);
    } catch (err) {
      // Some printers do not support ATT long writes.  If the very first
      // bitmap write fails before any label was completed, retry the whole
      // job with write-without-response.  The retry starts with ESC @ again,
      // which clears any partially buffered bitmap.
      const props = (char && char.properties) || {};
      if (mode === 'ack' && props.writeWithoutResponse && completedLabels === 0) {
        const result = await run('noack');
        result.fallback = 'noack';
        return result;
      }
      throw err;
    }
  }

  root.BLEPrintCore = {
    ESC: ESC, GS: GS, FF: FF,
    sleep: sleep,
    fillMap: fillMap,
    renderRaster: renderRaster,
    padLeft: padLeft,
    escPosBitmap: escPosBitmap,
    escPosFeedSegments: escPosFeedSegments,
    buildEscPosLabel: buildEscPosLabel,
    buildTsplLabel: buildTsplLabel,
    buildRawLabel: buildRawLabel,
    buildJob: buildJob,
    jobBytes: jobBytes,
    resolveWriteMode: resolveWriteMode,
    writeData: writeData,
    sendJob: sendJob
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
"""


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
  #export-bin { background: #7c3aed; color: #fff; }
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
      <label>标签间隙 (TSPL, mm) <input id="p-gap" type="number" step="0.1"></label>
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
      <label>BLE 写入模式
        <select id="p-writemode">
          <option value="auto">自动（优先应答）</option>
          <option value="ack">应答写入 (with response)</option>
          <option value="noack">无应答写入 (without response)</option>
        </select>
      </label>
      <label>块大小 (字节) <input id="p-chunk" type="number" min="16" max="512"></label>
      <label>块间延迟 (ms) <input id="p-chunkdelay" type="number" min="0"></label>
      <label>标签间隔 (ms) <input id="p-labeldelay" type="number" min="0"></label>
      <label>分段行数 (0=整张) <input id="p-bandrows" type="number" min="0"></label>
      <label class="check"><input id="p-invert" type="checkbox"> 反色</label>
      <label class="check"><input id="p-reset" type="checkbox"> 每张前复位 ESC @（旧行为，可能截断）</label>
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
      <button id="export-bin" disabled>导出指令流 (.bin)</button>
    </div>
    <div id="status"></div>
  </div>

  <div class="card">
    <h1 style="font-size:16px;">标签预览</h1>
    <div class="labels" id="preview"></div>
  </div>

<script>
__BLE_CORE_JS__
</script>
<script>
'use strict';

const CONFIG = __BLE_CONFIG__;
const LABELS = __BLE_LABELS__;
const Core = window.BLEPrintCore;

const $ = (id) => document.getElementById(id);
const status = (msg, cls) => { const el = $('status'); el.textContent = msg; el.className = cls || ''; };

// ---- initialise parameter inputs from server defaults ----
$('p-width').value = CONFIG.width_mm;
$('p-height').value = CONFIG.height_mm;
$('p-offx').value = CONFIG.offset_x;
$('p-offy').value = CONFIG.offset_y;
$('p-feed').value = CONFIG.feed_mm;
$('p-gap').value = CONFIG.gap_mm;
$('p-dpi').value = CONFIG.dpi;
$('p-thr').value = CONFIG.threshold;
$('p-proto').value = CONFIG.protocol;
$('p-invert').checked = !!CONFIG.invert;
$('p-fill').value = CONFIG.fill_mode || 'stretch';
$('p-writemode').value = CONFIG.write_mode || 'auto';
$('p-chunk').value = CONFIG.chunk_size;
$('p-chunkdelay').value = CONFIG.chunk_delay;
$('p-labeldelay').value = CONFIG.inter_label_delay;
$('p-bandrows').value = CONFIG.band_rows;
$('p-reset').checked = !!CONFIG.reset_each_label;

// Restore previously saved parameters (client-side persistence)
loadSavedParams();

function getParams() {
  return {
    width_mm: parseFloat($('p-width').value) || CONFIG.width_mm,
    height_mm: parseFloat($('p-height').value) || CONFIG.height_mm,
    offset_x: parseFloat($('p-offx').value) || 0,
    offset_y: parseFloat($('p-offy').value) || 0,
    feed_mm: parseFloat($('p-feed').value) || 0,
    gap_mm: parseFloat($('p-gap').value) || 0,
    dpi: parseInt($('p-dpi').value) || 203,
    threshold: parseInt($('p-thr').value) || 128,
    invert: $('p-invert').checked,
    protocol: $('p-proto').value,
    fill: $('p-fill').value,
    write_mode: $('p-writemode').value,
    chunk_size: parseInt($('p-chunk').value) || 180,
    chunk_delay: parseInt($('p-chunkdelay').value) || 0,
    inter_label_delay: parseInt($('p-labeldelay').value) || 0,
    band_rows: parseInt($('p-bandrows').value) || 0,
    reset_each_label: $('p-reset').checked
  };
}

// v2 key: v0.4.x stored chunk_size=200 / chunk_delay=15 here, which would
// silently override the new, safer defaults. Old entries are intentionally
// ignored; calibration values also live in the plugin settings on the server.
function loadSavedParams() {
  try {
    const raw = localStorage.getItem('ble-label-printer-params-v2');
    if (!raw) return false;
    const s = JSON.parse(raw);
    if (!s || typeof s !== 'object') return false;
    if (s.width_mm != null) $('p-width').value = s.width_mm;
    if (s.height_mm != null) $('p-height').value = s.height_mm;
    if (s.offset_x != null) $('p-offx').value = s.offset_x;
    if (s.offset_y != null) $('p-offy').value = s.offset_y;
    if (s.feed_mm != null) $('p-feed').value = s.feed_mm;
    if (s.gap_mm != null) $('p-gap').value = s.gap_mm;
    if (s.dpi != null) $('p-dpi').value = s.dpi;
    if (s.threshold != null) $('p-thr').value = s.threshold;
    if (s.invert != null) $('p-invert').checked = !!s.invert;
    if (s.protocol) $('p-proto').value = s.protocol;
    if (s.fill) $('p-fill').value = s.fill;
    if (s.write_mode) $('p-writemode').value = s.write_mode;
    if (s.chunk_size != null) $('p-chunk').value = s.chunk_size;
    if (s.chunk_delay != null) $('p-chunkdelay').value = s.chunk_delay;
    if (s.inter_label_delay != null) $('p-labeldelay').value = s.inter_label_delay;
    if (s.band_rows != null) $('p-bandrows').value = s.band_rows;
    if (s.reset_each_label != null) $('p-reset').checked = !!s.reset_each_label;
    return true;
  } catch (e) {
    return false;
  }
}

async function saveParams() {
  const p = getParams();
  // Always persist to this browser
  try {
    localStorage.setItem('ble-label-printer-params-v2', JSON.stringify(p));
  } catch (e) {}

  // Also persist to the plugin settings (server side)
  const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
  const map = {
    PROTOCOL: p.protocol,
    DPI: String(p.dpi),
    OFFSET_X: String(p.offset_x),
    OFFSET_Y: String(p.offset_y),
    FEED_MM: String(p.feed_mm),
    GAP_MM: String(p.gap_mm),
    THRESHOLD: String(p.threshold),
    INVERT: p.invert ? 'True' : 'False',
    FILL_MODE: p.fill,
    WRITE_MODE: p.write_mode,
    WRITE_CHUNK_SIZE: String(p.chunk_size),
    WRITE_CHUNK_DELAY: String(p.chunk_delay),
    INTER_LABEL_DELAY: String(p.inter_label_delay),
    BAND_ROWS: String(p.band_rows),
    RESET_EACH_LABEL: p.reset_each_label ? 'True' : 'False'
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
  if (c.properties.write && c.properties.writeWithoutResponse) rank = 30;
  else if (c.properties.write) rank = 25;              // preferred: ACKed writes
  else if (c.properties.writeWithoutResponse) rank = 20;
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

// ---- state ----
let device = null, selectedChar = null, writableChars = [];
let loadedLabels = [];   // [{filename, gray, w, h}]
let rendered = [];       // [{raster, dotsX, dotsY, bytesPerRow}]

function refresh() {
  const p = getParams();
  rendered = loadedLabels.map(l => Core.renderRaster(l.gray, l.w, l.h, p));
  $('export-bin').disabled = !rendered.length;
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

    const mode = selectedChar ? Core.resolveWriteMode(selectedChar, getParams().write_mode) : '';
    $('dev-status').textContent = '已连接: ' + (device.name || device.id) +
      (mode ? '（写入模式: ' + mode + '）' : '');
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

const sleep = Core.sleep;

async function writeRaw(data) {
  // Used by the "test feed" button only.
  const p = getParams();
  const mode = Core.resolveWriteMode(selectedChar, p.write_mode);
  await Core.writeData(selectedChar, data, {
    mode: mode, chunkSize: p.chunk_size, delay: p.chunk_delay
  });
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
    await writeRaw(new Uint8Array(out));
    status(msg, 'ok');
  } catch (e) {
    status('走纸指令发送失败: ' + (e.message || e), 'error');
  }
}

function buildCurrentJob(copies) {
  const p = getParams();
  const rasters = [];
  for (let c = 0; c < copies; c++) {
    for (const r of rendered) rasters.push(r);
  }
  return { p, job: Core.buildJob(rasters, p) };
}

async function print() {
  if (!selectedChar) { status('请先连接打印机。', 'error'); return; }
  const copies = Math.max(1, parseInt($('copies').value || '1', 10));
  const built = buildCurrentJob(copies);
  const p = built.p, job = built.job;
  try {
    $('print').disabled = true;
    $('export-bin').disabled = true;
    const lines = [];
    const result = await Core.sendJob({
      char: selectedChar,
      job: job,
      p: p,
      onProgress: (done, total, info) => {
        lines.push('标签 ' + done + '/' + total + ': ' + info.bytes + ' 字节, ' + info.ms + ' ms');
        status('正在打印 ' + done + '/' + total + ' ...\\n' + lines.slice(-4).join('\\n'));
      }
    });
    lines.push('总计 ' + result.bytes + ' 字节, ' + (result.ms / 1000).toFixed(1) + ' s, 写入模式 ' + result.mode +
      (result.fallback ? '（打印机不支持长写，已自动降级）' : ''));
    status('打印完成，共 ' + job.labels.length + ' 张标签。\\n' + lines.slice(-6).join('\\n'), 'ok');
  } catch (e) {
    status('打印出错: ' + (e.message || e) + '\\n已发送的标签可能不完整，请检查后重试。', 'error');
  } finally {
    $('print').disabled = false;
    $('export-bin').disabled = !rendered.length;
  }
}

function exportBin() {
  const copies = Math.max(1, parseInt($('copies').value || '1', 10));
  const built = buildCurrentJob(copies);
  const job = built.job;
  let size = Core.jobBytes(job);
  const buf = new Uint8Array(size);
  let off = 0;
  if (job.init && job.init.length) { buf.set(job.init, off); off += job.init.length; }
  job.labels.forEach(segs => segs.forEach(s => { buf.set(s, off); off += s.length; }));
  const blob = new Blob([buf], { type: 'application/octet-stream' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'ble-labels-' + built.p.protocol.replace('/', '-') + '.bin';
  a.click();
  status('已导出 ' + size + ' 字节指令流（' + job.labels.length + ' 张标签）。', 'ok');
}

// ---- wire up events ----
$('connect').addEventListener('click', connect);
$('disconnect').addEventListener('click', disconnect);
$('print').addEventListener('click', print);
$('test-feed').addEventListener('click', testFeed);
$('save-params').addEventListener('click', saveParams);
$('export-bin').addEventListener('click', exportBin);
$('char-select').addEventListener('change', (ev) => {
  const idx = parseInt(ev.target.value, 10);
  if (!isNaN(idx) && writableChars[idx]) selectedChar = writableChars[idx].characteristic;
});

['p-width', 'p-height', 'p-offx', 'p-offy', 'p-feed', 'p-gap', 'p-dpi', 'p-thr',
 'p-proto', 'p-fill', 'p-writemode', 'p-chunk', 'p-chunkdelay', 'p-labeldelay',
 'p-bandrows'].forEach(id => {
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
